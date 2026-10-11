"""Datasets and their items. Items refer to images by path or address; the image's hash is
recorded when the item is imported."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, JsonValue, StringConstraints

from labelflow.api.deps import AdminProject, RuntimeDep
from labelflow.api.errors import Unprocessable, errors
from labelflow.api.limits import as_size, json_size
from labelflow.api.media import CannotHash, content_hash
from labelflow.engine import Dataset, Engine, Item, NewItem, Page

router = APIRouter(
    prefix="/projects/{project}/datasets",
    tags=["admin: datasets"],
    responses=errors(401, 403, 404, 422),
)

MAX_PROBLEMS_SHOWN = 100

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DatasetIn(BaseModel):
    name: str = Field(
        max_length=64,
        description="The dataset name: letters, digits and underscores, starting with a letter.",
    )


class ItemIn(BaseModel):
    item: Text = Field(
        max_length=300, description="The item's id, unique in the dataset. Often the file name."
    )
    uri: Text = Field(
        max_length=2000,
        description="Where the image is: a `file://` address, a path inside the server's media "
        "folder, or any other address. The system only keeps the address.",
    )
    content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="The SHA-256 of the image file, as 64 hexadecimal characters. Leave it out "
        "only if the server has a media folder and the file is inside it: it then computes "
        "the hash itself. Without a media folder every item needs its hash.",
    )
    properties: dict[str, JsonValue] = Field(
        default={},
        description="Extra facts about the item, such as a gold-standard answer under the name "
        "a stage's `score_against` uses. Labelers are never shown them.",
    )


class ImportIn(BaseModel):
    items: list[ItemIn] = Field(
        min_length=1,
        description="The items to add. All of them are added, or none. How many fit in one "
        "batch is a server setting (10,000 unless changed).",
    )


class ImportOut(BaseModel):
    dataset: str = Field(description="The dataset the items were added to.")
    imported: int = Field(description="How many items were added.")


@router.get("", response_model=list[Dataset], summary="List datasets")
def list_datasets(ctx: AdminProject) -> list[Dataset]:
    """List the project's datasets with their number of items."""
    return ctx.engine.datasets()


@router.post(
    "", status_code=201, response_model=Dataset, summary="Create a dataset", responses=errors(409)
)
def create_dataset(body: DatasetIn, ctx: AdminProject) -> Dataset:
    """Create an empty dataset. Add items to it with the import endpoint."""
    ctx.engine.create_dataset(body.name, ctx.actor)
    return ctx.engine.dataset(body.name)


@router.post(
    "/{dataset}/items",
    status_code=201,
    response_model=ImportOut,
    summary="Import items",
    responses=errors(409, 413),
)
def import_items(dataset: str, body: ImportIn, ctx: AdminProject, runtime: RuntimeDep) -> ImportOut:
    """Add a batch of items to a dataset. An item without a `content_hash` is hashed by the
    server when its file is inside the media folder; otherwise it is refused. The batch is
    all or nothing: if any item has a problem, none is added and every problem is listed.
    Items added while a workflow runs over the dataset enter that workflow at once."""
    settings = runtime.settings
    if len(body.items) > settings.max_import_items:
        raise Unprocessable(
            f"The batch has {len(body.items)} items, where the server accepts at most "
            f"{settings.max_import_items} at a time. Send it in smaller batches."
        )
    existing = _item_ids(ctx.engine, dataset)  # also answers 404 for an unknown dataset
    problems: list[str] = []
    ready: list[NewItem] = []
    seen: set[str] = set()
    for entry in body.items:
        if entry.item in existing or entry.item in seen:
            where = "is already in the dataset" if entry.item in existing else "is listed twice"
            problems.append(f"Item '{entry.item}' {where}.")
            continue
        seen.add(entry.item)
        size = json_size(entry.properties)
        if size > settings.max_properties_bytes:
            problems.append(
                f"Item '{entry.item}': its properties are too large ({as_size(size)} as JSON, "
                f"where the limit is {as_size(settings.max_properties_bytes)})."
            )
            continue
        try:
            digest = entry.content_hash or content_hash(entry.item, entry.uri, settings.media_root)
        except CannotHash as error:
            problems.append(str(error))
            continue
        ready.append(
            NewItem(**entry.model_dump(exclude={"content_hash"}), content_hash=digest.lower())
        )
    if problems:
        shown = problems[:MAX_PROBLEMS_SHOWN]
        if len(problems) > len(shown):
            shown.append(f"...and {len(problems) - len(shown)} more.")
        raise Unprocessable(
            f"No items were imported: {len(problems)} of {len(body.items)} have problems.", shown
        )
    imported = ctx.engine.import_items(dataset, ready, ctx.actor)
    return ImportOut(dataset=dataset, imported=len(imported))


def _item_ids(engine: Engine, dataset: str) -> set[str]:
    """The ids of every item the dataset holds."""
    ids: set[str] = set()
    while True:
        page = engine.items(dataset, offset=len(ids), limit=1000)
        ids.update(item.item for item in page.rows)
        if not page.rows or len(ids) >= page.total:
            return ids


@router.get(
    "/{dataset}/items",
    response_model=Page[Item],
    summary="List a dataset's items",
    responses=errors(404),
)
def list_items(
    dataset: str,
    ctx: AdminProject,
    limit: Annotated[int, Query(ge=1, le=1000, description="How many items to return.")] = 100,
    offset: Annotated[int, Query(ge=0, description="How many items to skip.")] = 0,
) -> Page[Item]:
    """List a dataset's items in the order they were imported, one page at a time."""
    return ctx.engine.items(dataset, offset=offset, limit=limit)
