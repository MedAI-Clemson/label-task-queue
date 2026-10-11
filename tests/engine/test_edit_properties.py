"""Property test: random edits mixed into random work keep the log, the tables and the
versions consistent.

Each example runs a random list of commands on one workflow: asking for work and
submitting, cancelling, excluding, importing, and proposing one of several whole configs
(allowed or refused, depending on the state). Every proposal is previewed first; it is
activated when the preview allows it, and otherwise activation must be refused with nothing
logged. Afterwards:

- rebuilding from the log gives exactly the same tables, and the chain verifies;
- the versions in the log only go up, and each decision follows the version it carries:
  an admission uses that version's ``sample`` and ``labels_per_item``, a claim that
  version's roles, and a final label that version's ``final``;
- a removed stage receives no item after its removal;
- in every preview, each item is counted in exactly one group per stage.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from labelflow.engine import EditRefused, Engine, EngineError, rebuild, verify_chain
from labelflow.engine.queries import config_at
from labelflow.labeltask import LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask

USERS = ("u1", "u2", "u3", "u4", "u5")
GRANTS = {
    "u1": ("annotator",),
    "u2": ("annotator", "expert"),
    "u3": ("annotator",),
    "u4": ("expert", "trainee"),
    "u5": ("trainee",),
}
ANNOTATE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}
REVIEW = {"name": "review", "input": "annotate.agreed", "assign_to": "expert", "sample": 0.5}


def config(*stages, final, **top):
    return {
        "workflow": "wf",
        "dataset": "ds",
        "label_task": "standin@1",
        "stages": list(stages),
        "final": final,
    } | top


BOTH = ["adjudicate", "annotate"]
PROPOSALS = [
    config(ANNOTATE, ADJUDICATE, final=BOTH),
    config({**ANNOTATE, "labels_per_item": 3}, ADJUDICATE, final=BOTH),
    config(ANNOTATE, {**ADJUDICATE, "sample": 0.5}, final=BOTH),
    config(ANNOTATE, {**ADJUDICATE, "assign_to": ["expert", "trainee"]}, final=BOTH),
    config(ANNOTATE, ADJUDICATE, REVIEW, final=["adjudicate", "review", "annotate"]),
    config(ANNOTATE, final=["annotate"]),
    config(ANNOTATE, ADJUDICATE, final=["annotate", "adjudicate"]),
    config(ANNOTATE, {**ADJUDICATE, "show_prior": False, "score_against": "key"}, final=BOTH),
    config(ANNOTATE, ADJUDICATE, final=BOTH, dataset="other"),
]

KINDS = ("work",) * 6 + ("edit",) * 3 + ("cancel", "exclude", "import")
operations = st.lists(
    st.tuples(st.sampled_from(KINDS), st.integers(0, 99), st.integers(0, 99)),
    min_size=5,
    max_size=40,
)


def new_project(make_store, seed: int) -> Engine:
    engine = Engine(make_store(), LabelTaskRegistry([StandInLabelTask()]))
    for role in ("annotator", "expert", "trainee"):
        engine.define_role(role, "admin")
    for user, roles in GRANTS.items():
        for role in roles:
            engine.grant_role(user, role, "admin", "setup")
    engine.create_dataset("ds", "admin")
    for i in range(4):
        engine.import_item("ds", f"item{i}", f"file:///{i}.png", f"h{i}", {}, "admin")
    engine.start_workflow(PROPOSALS[0], seed, "admin")
    return engine


def run(engine: Engine, op: tuple) -> None:
    kind, a, b = op
    before = len(engine.store.events())
    if kind == "edit":
        proposal = PROPOSALS[a % len(PROPOSALS)]
        preview = engine.preview_edit("wf", proposal)
        assert len(engine.store.events()) == before, "a preview logged something"
        for impact in preview.stages:
            groups = impact.in_stage + impact.may_arrive + impact.past + impact.cannot_arrive
            assert groups == preview.items.entered
        if preview.ok:
            engine.activate_edit("wf", proposal, "admin", preview.base_version)
            assert len(engine.store.events()) == before + 1
        else:
            try:
                engine.activate_edit("wf", proposal, "admin", preview.base_version)
            except EditRefused as refused:
                assert refused.problems == list(preview.errors)
            else:
                raise AssertionError("an edit the preview refused was activated")
            assert len(engine.store.events()) == before
        return
    user = USERS[a % len(USERS)]
    try:
        if kind == "work":
            slot = engine.request_work("wf", user)
            if slot is not None:
                shown = engine.work_packet("wf", slot.slot, user).shown()
                engine.submit("wf", slot.slot, user, {"value": "ab"[b % 2]}, shown)
        elif kind == "cancel":
            engine.cancel_open_slots("wf", ("annotate", "adjudicate")[b % 2], "admin", "random")
        elif kind == "exclude":
            items = engine.store.select("workflow_items", workflow="wf")
            engine.exclude_item("wf", items[b % len(items)]["item"], "admin", "random")
        elif kind == "import":
            i = len(engine.store.select("items", dataset="ds"))
            engine.import_item("ds", f"item{i}", f"file:///{i}.png", f"h{i}", {}, "admin")
    except EngineError:
        assert len(engine.store.events()) == before


def check(engine: Engine) -> None:
    store = engine.store
    events = store.events()
    assert verify_chain(events) == len(events)
    assert rebuild(events).snapshot() == store.snapshot()

    versions = [e.config_version for e in events if e.workflow == "wf"]
    assert versions == sorted(versions)
    for e in events:
        if e.workflow != "wf" or e.type == "config_activated":
            continue
        version = config_at(store, "wf", e.config_version)
        if e.type == "admission_decided":
            # KeyError here would mean a stage received an item after its removal.
            stage = version.stage(e.stage)
            assert e.threshold == stage.sample
            assert e.labels_per_item == stage.model_dump()["labels_per_item"]
        elif e.type == "slot_claimed":
            assert set(e.roles) & set(version.stage(e.stage).roles)
        elif e.type == "item_finalized" and e.source_stage is not None:
            assert e.source_stage in version.resolved_final()


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        # make_store hands out a fresh store on every call, so sharing it is safe
        HealthCheck.function_scoped_fixture,
    ],
)
@given(seed=st.integers(0, 2**31), ops=operations)
def test_random_edits_keep_the_log_and_versions_consistent(make_store, seed, ops):
    engine = new_project(make_store, seed)
    for op in ops:
        run(engine, op)
    check(engine)
