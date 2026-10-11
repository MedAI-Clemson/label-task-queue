# label-task-queue

Auditable labeling workflows for research data. A researcher writes a short YAML config
that says:

- which stages an item goes through;
- who labels it at each stage;
- where its final label comes from.

Labelers ask for work and claim it. Every decision is recorded in an append-only event log,
together with the rule that caused it. This means you can always answer "why did this image
go to the expert?" and "where did this final label come from?"

This is the Phase 1 rewrite.

- [`docs/phase1-design-notes.md`](docs/phase1-design-notes.md) has the design.
- [`docs/phase1-implementation-notes.md`](docs/phase1-implementation-notes.md) has the
  status by milestone and the decisions that still need a yes or no.
- The earlier prototype is in the git history at `e77c9d0`.

## A workflow

```yaml
workflow: xray_main
dataset: xray_batch_1
label_task: image_ontology@1

stages:
  - name: annotate
    assign_to: annotator          # each image labeled once (the default)

  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2                   # 20% of annotated images

final: [expert_review, annotate]  # the review label wins where it exists
```

More examples are in [`examples/workflows/`](examples/workflows/). The published JSON Schema
for configs is [`schema/workflow-config.schema.json`](schema/workflow-config.schema.json).
A label task is defined by an ontology file such as
[`examples/ontologies/chest_xray.yaml`](examples/ontologies/chest_xray.yaml).

## Install

Requires Python 3.12.

```bash
python3.12 -m venv env
source env/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
./run.sh                                          # migrate the database, then serve on :8000
python -m labelflow.api.admin create-admin <username>   # once, to make the first administrator
```

`run.sh` runs `alembic upgrade head` before starting uvicorn. The API docs are at
`http://localhost:8000/docs`. With Docker, the container also migrates on start:

```bash
docker compose up --build
docker compose run --rm app sh -c \
  "alembic upgrade head && python -m labelflow.api.admin create-admin <username>"
```

Settings are environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URI` | `sqlite:///./db/labelflow.db` | The SQLite database. |
| `LABELFLOW_LABEL_TASKS` | `./ontologies` (`run.sh`: `examples/ontologies`) | Folder of ontology files. |
| `LABELFLOW_MEDIA_ROOT` | not set | Folder the server may read images from, to hash them at import. |
| `LABELFLOW_SESSION_HOURS` | `12` | How long a login lasts. |
| `LABELFLOW_COOKIE_SECURE` | `false` | Set to `true` when the service is served over https. |

A typical session over the API:

1. An admin creates accounts, a project and its roles.
2. The admin grants roles, creates a dataset and imports items. Each item is a reference
   (path or URL) plus its content hash.
3. The admin validates the workflow config and starts it.
4. Labelers call `POST /projects/{p}/workflows/{w}/next`, then `submit` or `release`.
5. Admins use `explain` on an item, read the score and sampling reports, and download the
   item and label exports as CSV or Parquet.

## Layout

| Package | What it holds |
| --- | --- |
| `src/labelflow/engine` | The labeling workflow: config and validation, stages, slots and claims, the event log, explain, edits to a running workflow. Plain Python, with no web or database code. |
| `src/labelflow/labeltask` | Label tasks: the built-in ontology label task, COCO RLE masks, and the consensus check. |
| `src/labelflow/store` | SQLite storage for the engine (SQLAlchemy 2), Alembic migrations, and the rebuild command. |
| `src/labelflow/api` | The web API (FastAPI): accounts and login, projects, workflows, labeler and admin endpoints, exports. |
| `src/labelflow/ui` | The annotation interface (not built yet). |

Useful commands:

```bash
python -m labelflow.engine.config.schema > schema/workflow-config.schema.json   # regenerate the schema
python -m labelflow.store.rebuild "$DATABASE_URI" --project <name> --verify-only  # check tables against the log
```

## Test

```bash
./test.sh           # ruff check, ruff format --check, pytest
```

The engine tests run twice, once on the in-memory store and once on SQLite. They include
Hypothesis property tests. For example, rebuilding from the log must reproduce the state
exactly, and nobody may label an item twice in a workflow.
