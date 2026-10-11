"""Rebuild or check a project's derived tables from its event log.

    python -m labelflow.store.rebuild sqlite:///labelflow.db --project xray
    python -m labelflow.store.rebuild sqlite:///labelflow.db --project xray --verify-only

Without ``--verify-only`` the project's derived rows are deleted and rebuilt by replaying its
events, in one transaction. With it, nothing changes: a fresh replay is compared with the
current tables and the differences are reported. Either way the fingerprint chain is
verified first, and a broken chain stops the command.

Exit status: 0 when done (or when the tables match the log), 1 when ``--verify-only`` found
differences, 2 when the log or the database could not be used.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from labelflow.engine import BrokenChain
from labelflow.store.connection import connect
from labelflow.store.sql import Report, SqlStore, projects


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m labelflow.store.rebuild",
        description="Rebuild a project's derived tables by replaying its event log.",
    )
    parser.add_argument("database", help="database URL, for example sqlite:///labelflow.db")
    parser.add_argument("--project", required=True, help="the project to rebuild")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="compare a fresh replay with the current tables and report; change nothing",
    )
    return parser.parse_args(argv)


def summary(project: str, report: Report, verb: str) -> str:
    return (
        f"Project '{project}': {report.events} events, fingerprint chain verified; "
        f"{verb} {report.rows} rows in the derived tables."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    try:
        url = sa.make_url(args.database)
        file = url.database if url.get_backend_name() == "sqlite" else None
        if file not in (None, "", ":memory:") and not Path(file).exists():
            raise ValueError(f"The database file {file} does not exist.")
        engine = connect(url)
    except (ValueError, sa.exc.ArgumentError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    try:
        known = projects(engine)
        if args.project not in known:
            print(
                f"Error: project '{args.project}' has no events in this database. "
                f"Projects here: {', '.join(known) or 'none'}.",
                file=sys.stderr,
            )
            return 2
        store = SqlStore(engine, args.project)
        if args.verify_only:
            report = store.verify_derived()
            if report.identical:
                print(summary(args.project, report, "a fresh replay matches"))
                return 0
            print(f"Project '{args.project}': the derived tables differ from the event log:")
            print("\n".join(f"  {line}" for line in report.differences))
            return 1
        report = store.rebuild_derived()
        print(summary(args.project, report, "rebuilt"))
        if report.identical:
            print("The tables already matched the log; nothing changed.")
        else:
            print("The rebuild corrected these differences:")
            print("\n".join(f"  {line}" for line in report.differences))
        return 0
    except BrokenChain as error:
        print(
            f"Error: the event log of project '{args.project}' is damaged. {error}", file=sys.stderr
        )
        print("Nothing was changed.", file=sys.stderr)
        return 2
    except sa.exc.OperationalError as error:
        print(f"Error: cannot use the database: {error.orig}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
