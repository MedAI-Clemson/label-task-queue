"""label-task-queue: auditable labeling workflows for research data.

Package layout:

- ``labelflow.engine``: the labeling workflow (config, stages, slots, claims, the event
  log, explain). Plain Python; knows nothing about FastAPI or the database.
- ``labelflow.labeltask``: label tasks (the ontology, validating labels, the consensus check).
- ``labelflow.store``: persistent storage for the engine (SQLAlchemy, SQLite).
- ``labelflow.api``: the web API (FastAPI).
- ``labelflow.ui``: the annotation interface.
"""

__version__ = "0.1.0.dev0"
