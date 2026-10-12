"""Set up a pilot kit on a running labelflow server, through its API.

Each kit (``pilot/montgomery``, ``pilot/jsrt``) has a small ``setup_pilot.py`` that describes
itself with a ``Kit`` and calls ``main`` here. In order, each step skipped when it is
already done, so the script can be run again:

1. log in as the admin (the password comes from ``LABELFLOW_ADMIN_PASSWORD`` or a prompt);
2. create the project and the roles ``trainee``, ``annotator`` and ``expert``;
3. with ``--accounts``, create the accounts listed in a CSV file (``username``,
   ``display_name``, ``roles`` separated by ``;``, and an optional ``password`` column)
   and grant their roles. The role ``admin`` makes a server administrator instead of
   granting a project role. Passwords that were not given are generated and appended to
   ``--credentials`` (default ``<kit>/out/credentials.csv``, git-ignored and readable by you
   only), with a warning to hand them out privately;
4. split the items of ``items.json`` (from the kit's ``prepare.py``) into the gold set
   (``--gold`` images, chosen by a hashed draw with ``--seed``, in proportion to the groups
   of the kit's stratifying property) and the main set (the rest), create the two datasets
   named by the workflow files, and import the items in batches;
5. check both workflow files and start them with ``--seed``. The training round starts only
   once someone holds ``trainee``, since it gives every image to the trainees who hold the
   role when the workflow starts.

It uses only the standard library (urllib) and PyYAML, which labelflow already needs.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ROLES = ("trainee", "annotator", "expert")
ADMIN = "admin"  # in the accounts CSV: a server administrator, not a project role
DEFAULT_SEED = 20261011


@dataclass(frozen=True)
class Kit:
    """What makes one pilot kit different from another."""

    title: str  # 'Montgomery', 'JSRT': used in messages and the role grants' reason
    folder: Path  # the kit's folder: workflows/, ontology/, out/ and README.md
    project: str  # the default project name
    stratify_by: str  # the item property the gold set is stratified on
    gold: int  # the default size of the gold set

    @property
    def path(self) -> str:
        """The kit's folder as the repository names it, such as 'pilot/montgomery'."""
        return f"pilot/{self.folder.name}"

    @property
    def workflows(self) -> tuple[Path, Path]:
        return (
            self.folder / "workflows" / "training.yaml",
            self.folder / "workflows" / "main.yaml",
        )

    @property
    def credentials(self) -> Path:
        return self.folder / "out" / "credentials.csv"

    @property
    def reason(self) -> str:
        return f"{self.title} pilot setup ({self.path}/setup_pilot.py)"


class SetupError(Exception):
    """A step failed; the message says which and why."""


class Api:
    """A small JSON client for the labelflow API, with the admin's login token."""

    def __init__(self, server: str, timeout: float = 120):
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None

    def send(self, method: str, path: str, body: Any = None, ok: Sequence[int] = ()) -> bytes:
        """Send a request and return the reply's body; empty for a status listed in ``ok``
        (such as 409, "already exists")."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.server + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in ok:
                return b""
            raise SetupError(f"{method} {path} failed ({error.code}): {_detail(error)}") from None
        except urllib.error.URLError as error:
            raise SetupError(f"Cannot reach the server at {self.server}: {error.reason}") from None

    def call(self, method: str, path: str, body: Any = None, ok: Sequence[int] = ()) -> Any:
        """Send a request and return the JSON reply; None when it is empty."""
        text = self.send(method, path, body, ok)
        return json.loads(text) if text else None

    def fetch(self, path: str) -> bytes:
        """GET a file, such as an export."""
        return self.send("GET", path)

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: Any = None, ok: Sequence[int] = ()) -> Any:
        return self.call("POST", path, body, ok)

    def login(self, username: str, password: str) -> dict[str, Any]:
        reply = self.post("/auth/login", {"username": username, "password": password})
        self.token = reply["token"]
        return reply


def _detail(error: urllib.error.HTTPError) -> str:
    try:
        reply = json.loads(error.read())
    except ValueError:
        return error.reason
    problems = reply.get("problems") or []
    return " ".join([str(reply.get("detail", "")), *(f"\n  - {p}" for p in problems)])


def admin_password(username: str, password: str | None = None) -> str:
    """The admin's password: given, from ``LABELFLOW_ADMIN_PASSWORD``, or asked for."""
    return (
        password
        or os.environ.get("LABELFLOW_ADMIN_PASSWORD")
        or getpass.getpass(f"Password for {username}: ")
    )


# --- Reading the inputs -------------------------------------------------------------------


def read_accounts(path: Path) -> list[dict[str, Any]]:
    """The accounts CSV: ``username``, ``display_name``, ``roles`` (``;``-separated: the
    project roles, or ``admin``), and an optional ``password`` column."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    people = []
    for number, row in enumerate(rows, start=2):
        username = (row.get("username") or "").strip()
        if not username:
            raise SetupError(f"{path.name}, line {number}: no username.")
        roles = [r.strip() for r in (row.get("roles") or "").replace(",", ";").split(";")]
        unknown = [r for r in roles if r and r not in (*ROLES, ADMIN)]
        if unknown:
            raise SetupError(
                f"{path.name}, line {number}: unknown role {unknown[0]!r}; the pilot's roles "
                f"are {', '.join(ROLES)}, and '{ADMIN}' makes a server administrator."
            )
        people.append(
            {
                "username": username,
                "display_name": (row.get("display_name") or "").strip(),
                "roles": [r for r in roles if r and r != ADMIN],
                "is_admin": ADMIN in roles,
                "password": (row.get("password") or "").strip(),
            }
        )
    return people


def split_items(
    items: list[dict[str, Any]], gold: int, seed: int, stratify_by: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The gold set and the main set. Items are grouped by their ``stratify_by`` property;
    within each group they are ordered by ``sha256(seed:item)``, each group gives the gold
    set its share of ``gold`` images (largest remainders), and the rest go to the main set.
    The same items and seed always give the same split."""
    if not 0 <= gold <= len(items):
        raise SetupError(f"--gold must be between 0 and {len(items)}, the number of items.")

    def rank(item: dict[str, Any]) -> str:
        return hashlib.sha256(f"{seed}:{item['item']}".encode()).hexdigest()

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item["properties"].get(stratify_by)), []).append(item)
    shares = {name: gold * len(group) / len(items) for name, group in groups.items()}
    counts = {name: int(share) for name, share in shares.items()}
    by_remainder = sorted(shares, key=lambda name: (counts[name] - shares[name], name))
    for name in by_remainder[: gold - sum(counts.values())]:
        counts[name] += 1
    chosen = set()
    for name, group in groups.items():
        chosen |= {item["item"] for item in sorted(group, key=rank)[: counts[name]]}
    gold_set = [item for item in items if item["item"] in chosen]
    main_set = [item for item in items if item["item"] not in chosen]
    return gold_set, main_set


def workflow_file(path: Path) -> tuple[str, str, str]:
    """The file's text, its workflow name and its dataset name."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return text, data["workflow"], data["dataset"]


# --- The steps ------------------------------------------------------------------------------


def setup(
    kit: Kit, args: argparse.Namespace, api: Api, password: str, say: Callable[[str], None]
) -> None:
    reply = api.login(args.admin, password)
    if not reply["user"]["is_admin"]:
        raise SetupError(f"'{args.admin}' is not an administrator.")
    project = args.project
    base = f"/projects/{quote(project)}"

    created = api.post("/projects", {"name": project}, ok=(409,))
    say(f"project {project}: {'created' if created else 'already there'}")
    for role in ROLES:
        created = api.post(f"{base}/roles", {"role": role}, ok=(409,))
        say(f"role {role}: {'defined' if created else 'already there'}")

    if args.accounts:
        make_accounts(kit, args, api, base, say)

    items = json.loads(args.items.read_text(encoding="utf-8"))
    gold_items, main_items = split_items(items, args.gold, args.seed, kit.stratify_by)
    files = [workflow_file(path) for path in args.workflows]
    datasets = [files[0][2], files[1][2]]
    for dataset, chosen in zip(datasets, (gold_items, main_items), strict=True):
        import_dataset(api, base, dataset, chosen, args.batch, say)

    holders = {r["role"]: r["holders"] for r in api.get(f"{base}/roles")}
    running = {w["workflow"] for w in api.get(f"{base}/workflows")}
    for (text, workflow, _), path in zip(files, args.workflows, strict=True):
        if workflow in running:
            say(f"workflow {workflow}: already running")
            continue
        report = api.post(f"{base}/workflows/validate", {"config": text})
        if not report["ok"]:
            raise SetupError(f"{path.name} is not valid here:\n{report['report']}")
        for warning in report["warnings"]:
            say(f"workflow {workflow}: warning at {warning['where']}: {warning['message']}")
        needs_all = [
            role
            for stage in yaml.safe_load(text)["stages"]
            if stage.get("labels_per_item") == "all"
            for role in _as_list(stage["assign_to"])
            if not holders.get(role)
        ]
        if needs_all:
            say(
                f"workflow {workflow}: NOT started, because nobody holds {needs_all[0]!r} yet "
                "and every image would be admitted with nobody to label it. Grant the role, "
                "then run this again."
            )
            continue
        started = api.post(f"{base}/workflows", {"config": text, "seed": args.seed})
        say(f"workflow {workflow}: started with seed {started['seed']}")
        for warning in started["warnings"]:
            say(f"workflow {workflow}: warning: {warning}")

    say("")
    say(next_steps(kit, args, [f[1] for f in files]))


def _as_list(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def make_accounts(
    kit: Kit, args: argparse.Namespace, api: Api, base: str, say: Callable[[str], None]
) -> None:
    people = read_accounts(args.accounts)
    existing = {a["user_id"]: a for a in api.get("/accounts")}
    new_credentials = []
    for person in people:
        name = person["username"]
        if name in existing:
            say(f"account {name}: already there (password unchanged)")
            if person["is_admin"] and not existing[name]["is_admin"]:
                say(f"  warning: {name} is listed as an admin but is not one; nothing changed")
            continue
        password = person["password"] or secrets.token_urlsafe(12)
        api.post(
            "/accounts",
            {
                "username": name,
                "password": password,
                "display_name": person["display_name"],
                "is_admin": person["is_admin"],
            },
        )
        if not person["password"]:
            new_credentials.append((name, password))
        say(f"account {name}: created{' (administrator)' if person['is_admin'] else ''}")
    if new_credentials:
        write_credentials(args.credentials, new_credentials)
        say(
            f"generated passwords appended to {args.credentials} (readable by you only).\n"
            "  WARNING: these are live passwords. Give each person theirs privately (not in a\n"
            "  shared channel or a group email), never commit the file, and delete it once\n"
            "  everyone has logged in."
        )

    holders = {r["role"]: set(r["holders"]) for r in api.get(f"{base}/roles")}
    for person in people:
        for role in person["roles"]:
            if person["username"] in holders.get(role, set()):
                continue
            reply = api.post(
                f"{base}/roles/{role}/grant", {"user": person["username"], "reason": kit.reason}
            )
            say(f"role {role}: granted to {person['username']}")
            for warning in reply["warnings"]:
                say(f"  warning: {warning}")


def write_credentials(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["username", "password"])
        writer.writerows(rows)
    os.chmod(path, 0o600)


def import_dataset(
    api: Api,
    base: str,
    dataset: str,
    items: list[dict[str, Any]],
    batch: int,
    say: Callable[[str], None],
) -> None:
    created = api.post(f"{base}/datasets", {"name": dataset}, ok=(409,))
    have: set[str] = set()
    while True:
        page = api.get(f"{base}/datasets/{dataset}/items?limit=1000&offset={len(have)}")
        have |= {row["item"] for row in page["rows"]}
        if not page["rows"] or len(have) >= page["total"]:
            break
    wanted = {item["item"] for item in items}
    if extra := sorted(have - wanted):
        raise SetupError(
            f"Dataset {dataset} holds items that this split does not put there "
            f"({', '.join(extra[:5])}...). Was it set up with another --seed or --gold?"
        )
    todo = [item for item in items if item["item"] not in have]
    for start in range(0, len(todo), batch):
        api.post(f"{base}/datasets/{dataset}/items", {"items": todo[start : start + batch]})
    state = "created" if created else "already there"
    say(f"dataset {dataset}: {state}; {len(todo)} items imported, {len(items)} in all")


def next_steps(kit: Kit, args: argparse.Namespace, workflows: list[str]) -> str:
    server, project = args.server.rstrip("/"), args.project
    return "\n".join(
        [
            "Next steps:",
            f"  - Everyone logs in at {server}/ui/ and picks a workflow:",
            f"      trainees: {workflows[0]} (all {args.gold} gold images each);",
            f"      annotators and experts: {workflows[1]}.",
            f"  - Watch progress at {server}/ui/admin/projects/{project}.",
            "  - When a trainee is ready, grant them 'annotator' (and revoke 'trainee') with a",
            "    reason, on the project page or with POST .../roles/annotator/grant.",
            f"  - At the end, export as described in {kit.path}/README.md.",
            f"  - Seed used for the split and the workflows: {args.seed}. Keep it.",
        ]
    )


def parse_args(kit: Kit, argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"python {kit.path}/setup_pilot.py",
        description=f"Set up the {kit.title} pilot (project, roles, accounts, datasets, items, "
        "workflows) on a running labelflow server. Safe to run again.",
    )
    parser.add_argument("--server", default="http://localhost:8000", help="the server's URL")
    parser.add_argument("--admin", required=True, help="an administrator's username")
    parser.add_argument("--items", type=Path, required=True, help="items.json from prepare.py")
    parser.add_argument(
        "--project", default=kit.project, help=f"project name (default: {kit.project})"
    )
    parser.add_argument(
        "--accounts", type=Path, help="CSV: username,display_name,roles (see the README)"
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=kit.credentials,
        help=f"where generated passwords are appended (default: {kit.path}/out/credentials.csv, "
        "which git ignores)",
    )
    parser.add_argument(
        "--gold", type=int, default=kit.gold, help=f"images in the gold set ({kit.gold})"
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="seed for the split and the workflows"
    )
    parser.add_argument("--batch", type=int, default=25, help="items per import request (25)")
    parser.add_argument(
        "--workflows",
        type=Path,
        nargs=2,
        default=list(kit.workflows),
        metavar=("TRAINING", "MAIN"),
        help=f"the training and main workflow files (default: {kit.path}/workflows/)",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.seed < 2**63:
        parser.error("--seed must be a whole number from 0 to 2**63 - 1")
    if args.batch < 1:
        parser.error("--batch must be at least 1")
    return args


def main(
    kit: Kit,
    argv: Sequence[str] | None = None,
    password: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    args = parse_args(kit, argv)
    try:
        setup(kit, args, Api(args.server), admin_password(args.admin, password), say)
    except (SetupError, OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
