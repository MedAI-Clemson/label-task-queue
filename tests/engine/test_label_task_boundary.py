"""The engine at the label task's edge: stored labels are never re-validated, an ontology
that changed under a running workflow is refused in plain words, and a label task that
crashes refuses the command instead of crashing it."""

import pytest

from labelflow.engine import EngineError, LabelTaskFailed, OntologyChanged, rebuild
from labelflow.labeltask import LabelValidationError
from labelflow.labeltask.standin import StandInLabelTask

DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
TRIPLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 3}
SCORED = {"name": "annotate", "assign_to": "annotator", "score_against": "gold_key"}


class EditableTask(StandInLabelTask):
    """A stand-in whose ontology file can be edited under a running workflow: with or
    without bumping its version, and so that values labeled earlier are no longer valid."""

    def __init__(self):
        super().__init__()
        self.ontology = "standin-ontology@1"
        self.rejects: set[str] = set()

    @property
    def ontology_version(self) -> str:
        return self.ontology

    def validate(self, label):
        label = super().validate(label)
        if label["value"] in self.rejects:
            raise LabelValidationError(
                [f"'{label['value']}' is no longer a value of the ontology."]
            )
        return label


def claim_and_submit(engine, user, value):
    slot = engine.request_work("wf", user)
    packet = engine.work_packet("wf", slot.slot, user)
    return slot, engine.submit("wf", slot.slot, user, {"value": value}, packet.shown())


def test_consensus_uses_stored_labels_without_validating_them_again(build, cfg):
    task = EditableTask()
    engine = build(cfg(DOUBLE), items=1, task=task)
    claim_and_submit(engine, "ana", "cat")
    task.rejects = {"cat"}  # the ontology file was edited, its version was not bumped
    slot = engine.request_work("wf", "ben")
    shown = engine.work_packet("wf", slot.slot, "ben").shown()
    with pytest.raises(EngineError, match="no longer a value"):  # new labels are still checked
        engine.submit("wf", slot.slot, "ben", {"value": "cat"}, shown)
    engine.submit("wf", slot.slot, "ben", {"value": "dog"}, shown)
    consensus = engine.store.get("consensus", workflow="wf", stage="annotate", item="img_001")
    assert consensus["agreed"] is False and consensus["detail"] == {"values": ["cat", "dog"]}
    assert engine.store.get("admissions", workflow="wf", stage="annotate", item="img_001")[
        "completed"
    ]
    assert rebuild(engine.store.events()).snapshot() == engine.store.snapshot()


def test_an_ontology_version_change_under_a_running_workflow_refuses_the_submit(build, cfg):
    task = EditableTask()
    engine = build(cfg(DOUBLE), items=1, task=task)
    claim_and_submit(engine, "ana", "cat")
    task.ontology = "standin-ontology@2"
    slot = engine.request_work("wf", "ben")
    shown = engine.work_packet("wf", slot.slot, "ben").shown()
    before_events, before_state = engine.store.events(), engine.store.snapshot()
    with pytest.raises(OntologyChanged) as caught:
        engine.submit("wf", slot.slot, "ben", {"value": "cat"}, shown)
    assert isinstance(caught.value, EngineError)
    assert str(caught.value) == (
        "Item img_001 cannot leave stage annotate: its labels there were made under ontology "
        "version standin-ontology@1, but label task standin@1 now has ontology version "
        "standin-ontology@2, so the labels cannot be compared. The ontology changed under a "
        "running workflow: bump the label task's version and start a new workflow for the "
        "new ontology. Nothing was recorded."
    )
    assert engine.store.events() == before_events
    assert engine.store.snapshot() == before_state
    assert engine.slot("wf", slot.slot).claimed_by == "ben"  # the claim is still held
    assert engine.request_work("wf", "ben").slot == slot.slot
    # Put the version back and the submit goes through.
    task.ontology = "standin-ontology@1"
    engine.submit("wf", slot.slot, "ben", {"value": "cat"}, shown)
    assert engine.store.get("final_labels", workflow="wf", item="img_001")["status"] == "final"


def test_an_ontology_version_change_also_refuses_a_cancel_that_would_compare_labels(build, cfg):
    task = EditableTask()
    engine = build(cfg(TRIPLE), items=1, task=task)
    claim_and_submit(engine, "ana", "cat")
    claim_and_submit(engine, "ben", "dog")
    task.ontology = "standin-ontology@2"
    before = engine.store.events()
    with pytest.raises(OntologyChanged, match="standin-ontology@1, but label task standin@1"):
        engine.cancel_open_slots("wf", "annotate", "admin", "enough labels")
    assert engine.store.events() == before
    assert engine.slot("wf", 3).status == "open"


def test_a_label_task_that_crashes_while_validating_refuses_the_submit(build, cfg):
    class Broken(StandInLabelTask):
        def validate(self, label):
            raise KeyError("shape")

    engine = build(cfg(DOUBLE), items=1, task=Broken())
    slot = engine.request_work("wf", "ana")
    shown = engine.work_packet("wf", slot.slot, "ana").shown()
    before = engine.store.events()
    with pytest.raises(LabelTaskFailed) as caught:
        engine.submit("wf", slot.slot, "ana", {"value": "cat"}, shown)
    assert str(caught.value) == (
        "Label task standin@1 failed while validating the label: KeyError: 'shape'. This is "
        "a problem in the label task, not in the request; nothing was recorded."
    )
    assert engine.store.events() == before and engine.slot("wf", slot.slot).claimed_by == "ana"


def test_a_label_task_that_crashes_while_scoring_refuses_the_submit(build, cfg):
    class Broken(StandInLabelTask):
        def score(self, label, key):
            raise ZeroDivisionError("no pixels")

    engine = build(
        cfg(SCORED), items=1, task=Broken(), properties=lambda i: {"gold_key": {"value": "cat"}}
    )
    slot = engine.request_work("wf", "ana")
    shown = engine.work_packet("wf", slot.slot, "ana").shown()
    before = engine.store.events()
    with pytest.raises(LabelTaskFailed, match="failed while scoring label 1 against the key"):
        engine.submit("wf", slot.slot, "ana", {"value": "cat"}, shown)
    assert engine.store.events() == before


def test_a_key_the_label_task_cannot_read_is_logged_as_invalid_and_never_blocks(build, cfg):
    class Picky(StandInLabelTask):
        def validate(self, label):
            if label.get("value") == "boom":
                raise KeyError("value")  # not a LabelValidationError
            return super().validate(label)

    engine = build(
        cfg(SCORED), items=1, task=Picky(), properties=lambda i: {"gold_key": {"value": "boom"}}
    )
    _, label_id = claim_and_submit(engine, "ana", "cat")
    score = engine.store.get("scores", label=label_id)
    assert score["key_status"] == "invalid_key" and "value" in score["detail"]["problem"]
