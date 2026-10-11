"""The labeling workflow engine: stages, slots, claims, the event log and explain.

Plain Python; it knows nothing about the web layer or the database. Typical use::

    store = InMemoryStore()
    engine = Engine(store, LabelTaskRegistry([my_label_task]))
    engine.define_role("annotator", actor="admin")
    ...
    slot = engine.request_work("xray_main", "ana")

A running workflow is edited with ``preview_edit`` and ``activate_edit`` (forward-only; see
``labelflow.engine.edits``). The config models live in ``labelflow.engine.config``.
"""

from labelflow.engine.draws import draw
from labelflow.engine.edits import Change, ChangePreview, FinalImpact, ItemCounts, StageImpact
from labelflow.engine.engine import Engine, utc_now
from labelflow.engine.errors import (
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
    Paused,
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
from labelflow.engine.events import (
    Event,
    Shown,
    canonical_json,
    event_from_json,
    event_hash,
    event_to_json,
    verify_chain,
)
from labelflow.engine.explain import Explanation
from labelflow.engine.reducer import apply_event
from labelflow.engine.store import InMemoryStore, Store, rebuild
from labelflow.engine.tables import SCHEMA, InMemoryTables, IntegrityError, Table, Tables
from labelflow.engine.views import (
    ConfigVersion,
    ConsensusShown,
    Dataset,
    Item,
    ItemProgress,
    NewItem,
    Page,
    Pause,
    PauseState,
    PriorLabel,
    Progress,
    Role,
    Slot,
    StageProgress,
    StageSummary,
    Workflow,
    WorkflowDetail,
    WorkPacket,
)

__all__ = [
    "SCHEMA",
    "AlreadyExists",
    "BrokenChain",
    "Change",
    "ChangePreview",
    "ConfigVersion",
    "ConsensusShown",
    "Dataset",
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
    "Item",
    "ItemCounts",
    "ItemProgress",
    "NewItem",
    "NotAllowed",
    "NotEligible",
    "NotFound",
    "Page",
    "Pause",
    "PauseState",
    "Paused",
    "PriorLabel",
    "Progress",
    "Role",
    "Shown",
    "Slot",
    "SlotNotYours",
    "StageImpact",
    "StageProgress",
    "StageSummary",
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
    "Workflow",
    "WorkflowDetail",
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
