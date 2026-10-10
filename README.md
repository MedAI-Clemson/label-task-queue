# label-task-queue

A headless REST backend (FastAPI + SQLModel on SQLite) that hands out data-labeling
tasks to users. It is an early-stage prototype; there is no UI or authentication.

## Concepts

- **Dataset**: a named collection of **Records** (arbitrary JSON `data`).
- **User**: a person with the role `Labeler` or `Admin`.
- **LabelQueue**: one registered dataset, a set of users and an ordered list of queue steps.
- **QueueStep**: a step of a labelqueue with a policy `type` and `num_records` capacity.
  Only the `distribute` policy is implemented (`policy_args: {"random": false}` assigns
  records in id order, `true` picks them at random). `consensus` and `priority` are
  placeholders and are rejected when creating a step.
- **Task**: a record assigned to a user under the active queue step (the lowest-ranked
  step that is not completed). `POST /labelqueues/{labelqueue_id}/{user_id}/task/`
  creates the next task, or answers `406 "Queue is empty."` when no records remain or
  the step has handed out `num_records` tasks.

The interactive API documentation is served at `/docs` once the app is running.

## Install

Requires Python 3.12.

```bash
python3.12 -m venv env
source env/bin/activate
pip install -r requirements-dev.txt   # or requirements.txt for runtime only
```

## Run

```bash
./run.sh            # uvicorn with --reload on port 8000, database in ./db/database.db
# or
uvicorn app.main:app
```

Configuration (environment variables):

- `DATABASE_URI`: SQLAlchemy URL, default `sqlite:///./database.db`.
- `DATABASE_ECHO`: set to `1` to log SQL statements.

Tables are created on startup. There are no migrations; after schema changes, delete
the database file.

With Docker:

```bash
docker compose up --build   # database is stored in ./db/database.db
```

## Test

```bash
./test.sh   # or: python -m pytest
```

Tests use an in-memory SQLite database and do not touch `DATABASE_URI`.
