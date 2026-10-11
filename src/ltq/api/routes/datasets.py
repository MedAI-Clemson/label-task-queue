"""Datasets and their items. Items refer to images by path or address; the image is hashed
when the item is imported."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, JsonValue, StringConstraints

from ltq.api import reads
from ltq.api.deps import AdminProject, RuntimeDep
from ltq.api.errors import Unprocessable, errors
from ltq.api.media import CannotHash, content_hash

router = APIRouter(
    prefix="/projects/{project}/datasets",
    tags=["admin: datasets"],
    responses=errors(401, 403, 404, 422),
)

MAX_BATCH = 10_000
MAX_PROBLEMS_SHOWN = 100

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DatasetIn(BaseModel):
    name: str = Field(
        max_length=64,
        description="The dataset name: letters, digits and underscores, starting with a letter.",
    )


class DatasetOut(BaseModel):
    dataset: str = Field(description="The dataset name.")
    created_by: str | None = Field(description="Who created the dataset.")
    created_at: datetime = Field(description="When the dataset was created (UTC).")
    items: int = Field(description="How many items the dataset holds.")


class ItemIn(BaseModel):
    item: Text = Field(
        max_length=300, description="The item's id, unique in the dataset. Often the file name."
    )
    uri: Text = Field(
        max_length=2000,
        description="Where the image is: a `file://` address, a path inside the media folder, "
        "or any other address. The system only keeps the address.",
    )
    content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="The SHA-256 of the image file, as 64 hexadecimal characters. Leave it out "
        "only if the server can read the file (a `file://` address, or a path inside the "
        "media folder): it then computes the hash itself.",
    )
    properties: dict[str, JsonValue] = Field(
        default={},
        description="Extra facts about the item, such as a gold-standard answer under the name "
        "a stage's `score_against` uses. Labelers are never shown them.",
    )


class ImportIn(BaseModel):
    items: list[ItemIn] = Field(
        min_length=1,
        max_length=MAX_BATCH,
        description=f"The items to add, at most {MAX_BATCH} at a time. All of them are added, "
        "or none.",
    )


class ImportOut(BaseModel):
    dataset: str = Field(description="The dataset the items were added to.")
    imported: int = Field(description="How many items were added.")


class ItemOut(BaseModel):
    item: str = Field(description="The item's id.")
    uri: str = Field(description="Where the image is.")
    content_hash: str = Field(description="The SHA-256 of the image, taken at import.")
    properties: dict[str, JsonValue] = Field(description="Extra facts about the item.")
    imported_by: str | None = Field(description="Who imported the item.")
    imported_at: datetime = Field(description="When the item was imported (UTC).")


class ItemPage(BaseModel):
    total: int = Field(description="How many items the dataset holds.")
    limit: int = Field(description="The page size asked for.")
    offset: int = Field(description="How many items were skipped.")
    items: list[ItemOut] = Field(description="This page of items, in the order they were imported.")


@router.get("", response_model=list[DatasetOut], summary="List datasets")
def list_datasets(ctx: AdminProject) -> list[DatasetOut]:
    """List the project's datasets with their number of items."""
    return [DatasetOut(**row) for row in reads.datasets(ctx.engine.store)]


@router.post(
    "",
    status_code=201,
    response_model=DatasetOut,
    summary="Create a dataset",
    responses=errors(409),
)
def create_dataset(body: DatasetIn, ctx: AdminProject) -> DatasetOut:
    """Create an empty dataset. Add items to it with the import endpoint."""
    ctx.engine.create_dataset(body.name, ctx.actor)
    return DatasetOut(**reads.dataset(ctx.engine.store, body.name))


@router.post(
    "/{dataset}/items",
    status_code=201,
    response_model=ImportOut,
    summary="Import items",
    responses=errors(409),
)
def import_items(dataset: str, body: ImportIn, ctx: AdminProject, runtime: RuntimeDep) -> ImportOut:
    """Add a batch of items to a dataset. If an item has no `content_hash` the server hashes
    the image when it can read it; otherwise the item is refused. The batch is all or
    nothing: if any item has a problem, none is added and every problem is listed. Items
    added while a workflow runs over the dataset enter that workflow at once."""
    store = ctx.engine.store
    existing = reads.item_ids(store, dataset)  # also answers 404 for an unknown dataset
    problems: list[str] = []
    ready: list[tuple[ItemIn, str]] = []
    seen: set[str] = set()
    for entry in body.items:
        if entry.item in existing or entry.item in seen:
            where = "is already in the dataset" if entry.item in existing else "is listed twice"
            problems.append(f"Item '{entry.item}' {where}.")
            continue
        seen.add(entry.item)
        try:
            digest = entry.content_hash or content_hash(
                entry.item, entry.uri, runtime.settings.media_root
            )
        except CannotHash as error:
            problems.append(str(error))
            continue
        ready.append((entry, digest.lower()))
    if problems:
        shown = problems[:MAX_PROBLEMS_SHOWN]
        if len(problems) > len(shown):
            shown.append(f"...and {len(problems) - len(shown)} more.")
        raise Unprocessable(
            f"No items were imported: {len(problems)} of {len(body.items)} have problems.", shown
        )
    with store.transaction():  # one transaction: if anything fails, nothing is imported
        for entry, digest in ready:
            ctx.engine.import_item(
                dataset, entry.item, entry.uri, digest, entry.properties, ctx.actor
            )
    return ImportOut(dataset=dataset, imported=len(ready))


@router.get(
    "/{dataset}/items",
    response_model=ItemPage,
    summary="List a dataset's items",
    responses=errors(404),
)
def list_items(
    dataset: str,
    ctx: AdminProject,
    limit: Annotated[int, Query(ge=1, le=1000, description="How many items to return.")] = 100,
    offset: Annotated[int, Query(ge=0, description="How many items to skip.")] = 0,
) -> ItemPage:
    """List a dataset's items in the order they were imported, one page at a time."""
    total, rows = reads.items_page(ctx.engine.store, dataset, limit, offset)
    return ItemPage(total=total, limit=limit, offset=offset, items=[ItemOut(**row) for row in rows])
