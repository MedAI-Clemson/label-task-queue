"""The labeling workflow engine: stages, slots, claims, the event log and explain.

Plain Python; it knows nothing about the web layer or the database. Typical use::

    store = InMemoryStore()
    engine = Engine(store, LabelTaskRegistry([my_label_task]))
    engine.define_role("annotator", actor="admin")
    ...
    slot = engine.request_work("xray_main", "ana")

A running workflow is edited with ``preview_edit`` and ``activate_edit`` (forward-only; see
``ltq.engine.edits``). The config models live in ``ltq.engine.config``.
"""

from ltq.engine.draws import draw
from ltq.engine.edits import Change, ChangePreview, FinalImpact, ItemCounts, StageImpact
from ltq.engine.engine import Engine, utc_now
from ltq.engine.errors import (
    AlreadyExists,
    BrokenChain,
    EditRefused,
    EngineError,
    InvalidLabel,
    InvalidRequest,
    InvalidShown,
    NotAllowed,
    NotEligible,
    NotFound,
    SlotNotYours,
    UnknownDataset,
    UnknownItem,
    UnknownLabelTaskRef,
    UnknownRole,
    UnknownSlot,
    UnknownStage,
    UnknownWorkflow,
    VersionConflict,
)
from ltq.engine.events import (
    Event,
    Shown,
    canonical_json,
    event_from_json,
    event_hash,
    event_to_json,
    verify_chain,
)
from ltq.engine.explain import Explanation
from ltq.engine.reducer import apply_event
from ltq.engine.store import InMemoryStore, Store, rebuild
from ltq.engine.tables import SCHEMA, InMemoryTables, IntegrityError, Table, Tables
from ltq.engine.views import ConsensusShown, PriorLabel, Slot, WorkPacket

__all__ = [
    "SCHEMA",
    "AlreadyExists",
    "BrokenChain",
    "Change",
    "ChangePreview",
    "ConsensusShown",
    "EditRefused",
    "Engine",
    "EngineError",
    "Event",
    "Explanation",
    "FinalImpact",
    "InMemoryStore",
    "InMemoryTables",
    "IntegrityError",
    "InvalidLabel",
    "InvalidRequest",
    "InvalidShown",
    "ItemCounts",
    "NotAllowed",
    "NotEligible",
    "NotFound",
    "PriorLabel",
    "Shown",
    "Slot",
    "SlotNotYours",
    "StageImpact",
    "Store",
    "Table",
    "Tables",
    "UnknownDataset",
    "UnknownItem",
    "UnknownLabelTaskRef",
    "UnknownRole",
    "UnknownSlot",
    "UnknownStage",
    "UnknownWorkflow",
    "VersionConflict",
    "WorkPacket",
    "apply_event",
    "canonical_json",
    "draw",
    "event_from_json",
    "event_hash",
    "event_to_json",
    "rebuild",
    "utc_now",
    "verify_chain",
]
