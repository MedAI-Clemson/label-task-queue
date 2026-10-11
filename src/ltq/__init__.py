"""label-task-queue: auditable labeling workflows for research data.

Package layout:

- ``ltq.engine``: the labeling workflow (config, stages, slots, claims, the event
  log, explain). Plain Python; knows nothing about FastAPI or the database.
- ``ltq.labeltask``: label tasks (the ontology, validating labels, the consensus check).
- ``ltq.store``: persistent storage for the engine (SQLAlchemy, SQLite).
- ``ltq.api``: the web API (FastAPI).
- ``ltq.ui``: the annotation interface.
"""

__version__ = "0.1.0.dev0"
