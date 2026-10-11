"""The two export tables as CSV and as Parquet.

The columns are listed here so an export has its header even when there are no rows, and a
Parquet column has its type even when every value is empty. ``tests/api`` checks that the
lists match the engine's rows. Times stay ISO 8601 text, exactly as logged; composite values
(a label, a list of ids) are JSON text, as the engine gives them.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from typing import Any

ITEM_COLUMNS: dict[str, str] = {
    "workflow": "text",
    "item": "text",
    "uri": "text",
    "content_hash": "text",
    "status": "text",
    "final_status": "text",
    "final_label": "text",
    "source_stage": "text",
    "label_ids": "text",
    "rule": "text",
    "config_version": "integer",
    "finalized_at": "text",
}

LABEL_COLUMNS: dict[str, str] = {
    "workflow": "text",
    "label_id": "integer",
    "item": "text",
    "stage": "text",
    "slot": "integer",
    "labeler": "text",
    "roles": "text",
    "roles_at_claim": "text",
    "config_version": "integer",
    "label_task": "text",
    "ontology_version": "text",
    "claimed_at": "text",
    "submitted_at": "text",
    "duration_ms": "integer",
    "shown_content_hash": "text",
    "shown_prior_ids": "text",
    "show_prior": "boolean",
    "shown_consensus_rule": "text",
    "score_against": "text",
    "score_key_status": "text",
    "score_agreed": "boolean",
    "score_rule_version": "text",
    "label": "text",
}

CSV = "text/csv; charset=utf-8"
PARQUET = "application/vnd.apache.parquet"

Rows = Sequence[Mapping[str, Any]]


def _check(rows: Rows, columns: Mapping[str, str]) -> None:
    for row in rows:
        unexpected = set(row) - set(columns)
        if unexpected:
            raise RuntimeError(f"The export has columns the API does not know: {unexpected}.")


def to_csv(rows: Rows, columns: Mapping[str, str]) -> bytes:
    """CSV with a header line; an empty value is an empty field, booleans are true or false."""
    _check(rows, columns)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(name)) for name in columns])
    return out.getvalue().encode("utf-8")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def to_parquet(rows: Rows, columns: Mapping[str, str]) -> bytes:
    """A Parquet file with one typed column per export column."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _check(rows, columns)
    types = {"text": pa.string(), "integer": pa.int64(), "boolean": pa.bool_()}
    schema = pa.schema([(name, types[kind]) for name, kind in columns.items()])
    table = pa.Table.from_pylist([dict(row) for row in rows], schema=schema)
    out = io.BytesIO()
    pq.write_table(table, out)
    return out.getvalue()
