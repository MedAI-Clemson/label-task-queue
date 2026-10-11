# label-task-queue

Auditable labeling workflows for research data. A researcher writes a short YAML
config that says which stages an item goes through, who labels it at each stage, and
where its final label comes from. Every decision is recorded in an append-only event log
with the rule that caused it.

This is the Phase 1 rewrite; see [`docs/phase1-design-notes.md`](docs/phase1-design-notes.md)
for the design. The earlier prototype lives in the git history (tag `v0-prototype`).

## Layout

| Package | What it holds |
| --- | --- |
| `src/ltq/engine` | The labeling workflow: config, stages, slots, claims, the event log, explain. Plain Python. |
| `src/ltq/labeltask` | Label tasks: the ontology, validating labels, the consensus check. |
| `src/ltq/store` | Storage for the engine (SQLAlchemy 2, SQLite). |
| `src/ltq/api` | The web API (FastAPI). |
| `src/ltq/ui` | The annotation interface. |

## Install

Requires Python 3.12.

```bash
python3.12 -m venv env
source env/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
./run.sh            # uvicorn with --reload on port 8000
# or
docker compose up --build
```

## Test

```bash
./test.sh           # ruff check, ruff format --check, pytest
```
