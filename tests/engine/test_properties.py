"""Property tests: random sequences of commands keep every invariant.

Each example starts a small project with one or two workflows (chosen from the configs
below, which cover labels_per_item 2/3/all/fraction, sample < 1, agreed and disagreed
branches, score_against and final: none), then runs a random list of commands: role grants
and revokes, imports, requests for work, explicit claims, submits (valid and invalid),
releases, cancels, exclusions (also of finalized items), and pauses and resumes of stages and
whole workflows. Each request for work is checked against the eligibility rules: it hands out
nothing only when nothing is claimable, and a slot the person released only when nothing
else is. Afterwards:

- rebuilding from the log gives exactly the same tables;
- the fingerprint chain verifies;
- nobody labeled an item twice in a workflow;
- no slot was ever claimed twice, and every slot is in exactly one consistent state;
- no slot was claimed while its stage or its workflow was paused;
- a refused command appended nothing.
"""

from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from labelflow.engine import (
    BrokenChain,
    Engine,
    EngineError,
    InvalidLabel,
    rebuild,
    verify_chain,
)
from labelflow.engine.config import WorkflowConfig
from labelflow.engine.eligibility import load_person, offered_last, why_not
from labelflow.engine.queries import active_config, active_pause, live_slots
from labelflow.labeltask import LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask

USERS = ("u1", "u2", "u3", "u4", "u5")
ROLES = ("annotator", "expert", "trainee")
VALUES = ("a", "b")
GRANTS = {
    "u1": ("annotator",),
    "u2": ("annotator", "expert"),
    "u3": ("annotator",),
    "u4": ("expert", "trainee"),
    "u5": ("trainee",),
}


def stage(name, roles, **settings):
    return {"name": name, "assign_to": roles, **settings}


CONFIGS = {
    "review": {
        "stages": [
            stage("annotate", "annotator"),
            stage("expert_review", "expert", input="annotate", sample=0.5),
        ],
        "final": ["expert_review", "annotate"],
    },
    "double": {
        "stages": [
            stage("annotate", "annotator", labels_per_item=2),
            stage("adjudicate", "expert", input="annotate.disagreed"),
        ],
        "final": ["adjudicate", "annotate"],
    },
    "fraction": {
        "stages": [
            stage(
                "annotate", "annotator", sample=0.7, labels_per_item={"count": 3, "fraction": 0.5}
            ),
            stage(
                "adjudicate",
                ["expert", "trainee"],
                input="annotate.disagreed",
                labels_per_item="all",
            ),
        ],
        "final": ["adjudicate", "annotate"],
    },
    "triple": {
        "stages": [
            stage("annotate", "annotator", labels_per_item=3, show_prior=False),
            stage("check", "expert", input="annotate.agreed", sample=0.5, score_against="gold_key"),
            stage("adjudicate", "expert", input="annotate.disagreed"),
        ],
        "final": ["adjudicate", "check", "annotate"],
    },
    "training": {
        "stages": [stage("gold_read", "trainee", labels_per_item="all", score_against="gold_key")],
        "final": "none",
    },
}

# Commands, weighted so that items often get far enough to reach consensus and finalize.
# "drain" lets every person ask for work and submit once.
KINDS = (
    ("work",) * 4
    + ("drain",) * 2
    + ("request",) * 3
    + ("submit", "submit", "release", "release")
    + ("grant", "revoke", "import", "cancel", "exclude", "invalid")
    + ("claim", "pause", "resume")
)
operations = st.lists(
    st.tuples(st.sampled_from(KINDS), st.integers(0, 99), st.integers(0, 99), st.integers(0, 99)),
    min_size=10,
    max_size=60,
)


def config_for(name: str, workflow: str) -> dict:
    return {"workflow": workflow, "dataset": "ds", "label_task": "standin@1", **CONFIGS[name]}


def gold(i: int) -> dict:
    return {} if i % 3 == 2 else {"gold_key": {"value": VALUES[i % 2]}}


def new_project(make_store, config_names, seed) -> tuple[Engine, list[str]]:
    engine = Engine(make_store(), LabelTaskRegistry([StandInLabelTask()]))
    for role in ROLES:
        engine.define_role(role, "admin")
    for user, roles in GRANTS.items():
        for role in roles:
            engine.grant_role(user, role, "admin", "setup")
    engine.create_dataset("ds", "admin")
    for i in range(3):
        engine.import_item("ds", f"item{i}", f"file:///{i}.png", f"h{i}", gold(i), "admin")
    workflows = []
    for n, name in enumerate(config_names):
        workflow = f"w{n}_{name}"
        engine.start_workflow(config_for(name, workflow), seed + n, "admin")
        workflows.append(workflow)
    return engine, workflows


def my_claim(engine, workflow, user):
    rows = engine.store.select("slots", workflow=workflow, status="claimed", claimed_by=user)
    return rows[0] if rows else None


def claimable(engine: Engine, workflow: str, user: str) -> list[dict]:
    """The open slots the person may claim now: eligible, and not in a paused stage or
    workflow (what ``request_work`` chooses from, worked out separately)."""
    store = engine.store
    if active_pause(store, workflow) is not None:
        return []
    _, config = active_config(store, workflow)
    person = load_person(store, workflow, user)
    return [
        slot
        for slot in store.select("slots", workflow=workflow, status="open")
        if slot["stage"] in config.stage_names
        and active_pause(store, workflow, slot["stage"]) is None
        and why_not(person, slot, config.stage(slot["stage"]).roles) is None
    ]


def checked_request(engine: Engine, workflow: str, user: str):
    """``request_work``, checked: None only when nothing is claimable, and a slot the
    person released only when every claimable slot is one they released."""
    held = my_claim(engine, workflow, user)
    options = [] if held else claimable(engine, workflow, user)
    person = load_person(engine.store, workflow, user)
    slot = engine.request_work(workflow, user)
    if held:
        assert slot is not None and slot.slot == held["slot"]
    elif slot is None:
        assert not options, f"{user} got no work but could claim {options}"
    else:
        assert slot.slot in {o["slot"] for o in options}
        if offered_last(person, {"stage": slot.stage, "item": slot.item}):
            assert all(offered_last(person, o) for o in options)
    return slot


def pause_or_resume(engine: Engine, workflow: str, kind: str, b: int) -> None:
    """Pause or resume the whole workflow (one time in three) or one of its stages."""
    stages = [s["name"] for s in CONFIGS[workflow.split("_", 1)[1]]["stages"]]
    reason = f"random {kind}"
    if b % 3 == 0:
        command = engine.pause_workflow if kind == "pause" else engine.resume_workflow
        command(workflow, "admin", reason)
    else:
        command = engine.pause_stage if kind == "pause" else engine.resume_stage
        command(workflow, stages[b % len(stages)], "admin", reason)


def run(engine: Engine, workflows: list[str], op: tuple) -> None:
    """Carry out one random command. Refusals are fine; they must leave no trace."""
    kind, a, b, c = op
    if kind == "drain":
        for i in range(len(USERS)):
            run(engine, workflows, ("work", i, b + i, c))
        return
    workflow = workflows[c % len(workflows)]
    user = USERS[a % len(USERS)]
    if kind in ("submit", "release", "invalid"):
        # Act as one of the people holding a claim, if anyone does.
        claimants = engine.store.select("slots", workflow=workflow, status="claimed")
        if claimants:
            user = claimants[a % len(claimants)]["claimed_by"]
    value = VALUES[b % len(VALUES)]
    before = len(engine.store.events())
    try:
        if kind == "grant":
            engine.grant_role(user, ROLES[b % len(ROLES)], "admin", "random grant")
        elif kind == "revoke":
            engine.revoke_role(user, ROLES[b % len(ROLES)], "admin", "random revoke")
        elif kind == "import":
            i = len(engine.store.select("items", dataset="ds"))
            engine.import_item("ds", f"item{i}", f"file:///{i}.png", f"h{i}", gold(i), "admin")
        elif kind == "request":
            checked_request(engine, workflow, user)
        elif kind == "claim":
            open_slots = engine.store.select("slots", workflow=workflow, status="open")
            if open_slots:
                engine.claim(workflow, open_slots[b % len(open_slots)]["slot"], user)
        elif kind in ("pause", "resume"):
            pause_or_resume(engine, workflow, kind, b)
        elif kind in ("work", "submit", "invalid"):
            claim = my_claim(engine, workflow, user)
            if claim is None and kind == "work":
                slot = checked_request(engine, workflow, user)
                claim = slot and {"slot": slot.slot}
            if claim is None:
                return
            shown = engine.work_packet(workflow, claim["slot"], user).shown()
            if kind == "invalid":
                try:
                    engine.submit(workflow, claim["slot"], user, {"value": ""}, shown)
                except InvalidLabel:
                    assert len(engine.store.events()) == before
                    return
                raise AssertionError("an invalid label was accepted")
            engine.submit(workflow, claim["slot"], user, {"value": value}, shown, b * 100)
        elif kind == "release":
            claim = my_claim(engine, workflow, user) or engine.request_work(workflow, user)
            if claim is not None:
                slot = claim["slot"] if isinstance(claim, dict) else claim.slot
                engine.release(workflow, slot, user, "random release")
        elif kind == "cancel":
            stages = [s["name"] for s in CONFIGS[workflow.split("_", 1)[1]]["stages"]]
            engine.cancel_open_slots(workflow, stages[b % len(stages)], "admin", "random cancel")
        elif kind == "exclude":
            items = engine.store.select("workflow_items", workflow=workflow)
            item = items[b % len(items)]["item"]
            engine.exclude_item(workflow, item, "admin", "random exclusion")
    except EngineError:
        assert len(engine.store.events()) == before, f"{kind} was refused but logged events"


def check_invariants(engine: Engine, workflows: list[str]) -> None:
    store = engine.store
    events = store.events()

    # The log rebuilds the same state, and its chain verifies.
    assert rebuild(events).snapshot() == store.snapshot()
    assert verify_chain(events) == len(events)

    for workflow in workflows:
        config = WorkflowConfig.model_validate(config_for(workflow.split("_", 1)[1], workflow))
        labels = store.select("labels", workflow=workflow)
        slots = {s["slot"]: s for s in store.select("slots", workflow=workflow)}

        # Nobody labels an item twice in a workflow; one label per slot.
        assert not [k for k, n in Counter((r["item"], r["user"]) for r in labels).items() if n > 1]
        assert len({r["slot"] for r in labels}) == len(labels)

        # Every slot is in exactly one consistent state.
        for slot in slots.values():
            status = slot["status"]
            assert status in ("open", "claimed", "filled", "cancelled")
            # claimed_by names the claimant while claimed, and who filled the slot after.
            assert (slot["claimed_by"] is not None) == (status in ("claimed", "filled"))
            assert (slot["label"] is not None) == (status == "filled")
            if status == "filled":
                label = store.get("labels", label=slot["label"])
                assert (label["slot"], label["item"]) == (slot["slot"], slot["item"])
            if slot["reserved_for"] and status == "filled":
                assert store.get("labels", label=slot["label"])["user"] == slot["reserved_for"]

        # At most one claim per person per workflow.
        claims = Counter(s["claimed_by"] for s in slots.values() if s["status"] == "claimed")
        assert all(n == 1 for n in claims.values())

        # Items: done items have their final record; waiting items have a live slot.
        for row in store.select("workflow_items", workflow=workflow):
            live = live_slots(store, workflow, row["item"])
            final = store.get("final_labels", workflow=workflow, item=row["item"])
            if row["status"] == "in_progress":
                assert live and final is None
            elif row["status"] == "excluded":
                assert not live  # a final label from before the exclusion is kept
            else:
                assert not live and final is not None
                if config.final == "none":
                    assert final["status"] == "none"

    # From the log: no slot is claimed while claimed, nor while paused; a claimant held a
    # stage role then.
    state: dict[int, str] = {}
    paused: set[tuple[str, str | None]] = set()
    for e in events:
        if e.type in ("workflow_paused", "stage_paused"):
            paused.add((e.workflow, e.stage))
        elif e.type in ("workflow_resumed", "stage_resumed"):
            paused.remove((e.workflow, e.stage))
        if e.type == "admission_decided":
            for new in e.slots:
                state[new.slot] = "open"
        elif e.type == "slot_claimed":
            assert state[e.slot] == "open", f"slot {e.slot} claimed twice"
            assert not {(e.workflow, None), (e.workflow, e.stage)} & paused, "claimed in a pause"
            state[e.slot] = "claimed"
            config = WorkflowConfig.model_validate(
                config_for(e.workflow.split("_", 1)[1], e.workflow)
            )
            assert set(e.roles) & set(config.stage(e.stage).roles)
        elif e.type == "slot_released":
            assert state[e.slot] == "claimed"
            state[e.slot] = "open"
        elif e.type == "label_submitted":
            assert state[e.slot] == "claimed"
            state[e.slot] = "filled"
        elif e.type in ("slots_cancelled", "item_excluded"):
            for slot in e.slots if e.type == "slots_cancelled" else e.cancelled_slots:
                assert state[slot] in ("open", "claimed")
                state[slot] = "cancelled"


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        # make_store hands out a fresh store on every call, so sharing it is safe
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    config_names=st.lists(st.sampled_from(sorted(CONFIGS)), min_size=1, max_size=2, unique=True),
    seed=st.integers(0, 2**31),
    ops=operations,
)
def test_random_commands_keep_every_invariant(make_store, config_names, seed, ops):
    engine, workflows = new_project(make_store, config_names, seed)
    for op in ops:
        run(engine, workflows, op)
    check_invariants(engine, workflows)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    config_name=st.sampled_from(sorted(CONFIGS)),
    ops=operations,
    data=st.data(),
)
def test_tampering_with_any_event_breaks_the_chain(make_store, config_name, ops, data):
    engine, workflows = new_project(make_store, [config_name], 7)
    for op in ops:
        run(engine, workflows, op)
    events = engine.store.events()
    position = data.draw(st.integers(0, len(events) - 1))
    field, value = data.draw(
        st.sampled_from(
            [("because", "edited"), ("actor", "mallory"), ("at", events[0].at.replace(year=2000))]
        )
    )
    tampered = list(events)
    tampered[position] = events[position].model_copy(update={field: value})
    try:
        verify_chain(tampered)
    except BrokenChain:
        pass
    else:
        raise AssertionError(f"changing {field} of event {position + 1} went unnoticed")


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    low=st.floats(0.05, 0.95),
    extra=st.floats(0.01, 0.5),
    seed=st.integers(0, 2**31),
)
def test_admission_rises_with_the_fraction(make_store, low, extra, seed):
    """Items admitted at sample f are admitted at every larger f'."""
    high = min(1.0, low + extra)
    admitted = []
    for fraction in (low, high):
        engine = Engine(make_store(), LabelTaskRegistry([StandInLabelTask()]))
        engine.define_role("annotator", "admin")
        engine.create_dataset("ds", "admin")
        for i in range(20):
            engine.import_item("ds", f"item{i}", f"file:///{i}.png", f"h{i}", {}, "admin")
        config = config_for("review", "wf")
        config["stages"] = [stage("annotate", "annotator", sample=fraction)]
        config["final"] = ["annotate"]
        engine.start_workflow(config, seed, "admin")
        admitted.append(
            {r["item"] for r in engine.store.select("admissions", workflow="wf") if r["admitted"]}
        )
    assert admitted[0] <= admitted[1]
