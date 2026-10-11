"""The labeling workflow config: the document a researcher writes.

A config is the single source of truth for a workflow. It is written as YAML and
parsed into these models. The models are immutable, and they reject unknown keys so
that an invented key is caught rather than silently ignored.

Example::

    workflow: xray_main
    dataset: xray_batch_1
    label_task: image_ontology@1

    stages:
      - name: annotate
        assign_to: annotator

      - name: expert_review
        input: annotate
        assign_to: expert
        sample: 0.2

    final: [expert_review, annotate]
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
INPUT_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(\.(agreed|disagreed))?$"
LABEL_TASK_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*@[0-9]+$"

Name = Annotated[str, Field(pattern=NAME_PATTERN)]
Outcome = Literal["all", "agreed", "disagreed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelsFraction(_Model):
    """A fraction of admitted items get several labels; the rest get one.

    ``{count: 3, fraction: 0.2}`` means 20% of admitted items get 3 labels and the
    rest get 1. Which items get 3 is decided by a hashed draw.
    """

    count: StrictInt = Field(ge=2, description="Labels for the items chosen by the draw.")
    fraction: float = Field(gt=0, lt=1, description="Share of admitted items that get `count`.")


LabelsPerItem = Annotated[StrictInt, Field(ge=1)] | Literal["all"] | LabelsFraction


class InputRef(_Model):
    """A parsed stage input: the upstream stage and which of its outputs."""

    stage: str
    outcome: Outcome = "all"

    def __str__(self) -> str:
        return self.stage if self.outcome == "all" else f"{self.stage}.{self.outcome}"


class Stage(_Model):
    """One stage of a workflow: its input, the roles assigned to it, and its settings."""

    name: Name = Field(description="The stage name, unique in the workflow.")
    input: Annotated[str, Field(pattern=INPUT_PATTERN)] | None = Field(
        None,
        description=(
            "Where the stage takes items from: an earlier stage (`annotate`), only the "
            "items whose labels agreed (`annotate.agreed`) or disagreed "
            "(`annotate.disagreed`). Omit it to take every item of the dataset."
        ),
    )
    assign_to: Name | tuple[Name, ...] = Field(
        description="The role, or list of roles, whose holders may claim this stage's slots."
    )
    sample: float = Field(
        1.0,
        gt=0,
        le=1,
        description="The fraction of arriving items admitted, by hashed draw. Default: all.",
    )
    labels_per_item: LabelsPerItem = Field(
        1,
        description=(
            "How many different people label each admitted item: a number, "
            "`{count, fraction}`, or `all` (everyone holding the roles at admission)."
        ),
    )
    score_against: Name | None = Field(
        None,
        description="An item property holding a key (such as a gold-standard answer) "
        "that each label is scored against. It changes no routing.",
    )
    show_prior: bool = Field(
        True,
        description="Whether labelers see earlier labels for the item. "
        "`false` makes the stage blind.",
    )

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles assigned to the stage, always as a tuple."""
        return (self.assign_to,) if isinstance(self.assign_to, str) else self.assign_to

    @property
    def input_ref(self) -> InputRef | None:
        """The parsed input, or None when the stage takes items from the dataset."""
        if self.input is None:
            return None
        stage, _, outcome = self.input.partition(".")
        return InputRef(stage=stage, outcome=outcome or "all")


class WorkflowConfig(_Model):
    """A whole labeling workflow config."""

    workflow: Name = Field(description="The workflow name, unique in the project.")
    dataset: Name = Field(description="The dataset whose items enter the workflow.")
    label_task: Annotated[str, Field(pattern=LABEL_TASK_PATTERN)] = Field(
        description="The label task, as `name@version`, for example `image_ontology@1`."
    )
    stages: tuple[Stage, ...] = Field(min_length=1, description="The stages, in order.")
    final: tuple[Name, ...] | Literal["none"] | None = Field(
        None,
        description=(
            "Where final labels come from: an ordered list of stages, where the first "
            "stage that labeled an item supplies its final label. `none` means the "
            "workflow produces no final labels. Default: every stage, last-listed first."
        ),
    )

    def stage(self, name: str) -> Stage:
        """Return the stage with this name. Raises KeyError if there is none."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages)

    def resolved_final(self) -> tuple[str, ...] | None:
        """The final step spelled out; None when the workflow declares ``final: none``."""
        if self.final == "none":
            return None
        if self.final is None:
            return tuple(reversed(self.stage_names))
        return self.final

    def downstream(self, stage: str, outcome: Outcome) -> tuple[Stage, ...]:
        """The stages, in config order, whose input is this output of ``stage``."""
        return tuple(
            s
            for s in self.stages
            if (ref := s.input_ref) is not None and ref.stage == stage and ref.outcome == outcome
        )

    @property
    def root_stages(self) -> tuple[Stage, ...]:
        """The stages that take items straight from the dataset."""
        return tuple(s for s in self.stages if s.input is None)
