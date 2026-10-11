"""The graph rules: how stages connect, and whether every item gets a final label.

The rules:

1. Stage names are unique.
2. A stage's input names a stage listed *earlier*. Stages are listed in order, so there
   are no cycles and no forward references.
3. ``.agreed`` and ``.disagreed`` need a stage that can have agreement, so its
   ``labels_per_item`` must not be exactly 1.
4. ``final`` is a non-empty list of distinct stage names.
5. Coverage: every stage either is listed in ``final`` or passes all of its items on to
   a later stage. Otherwise some items could finish with no final label.
6. Two stages that can hold the same item at the same time (*parallel* stages: neither
   takes its items from the other, and no upstream agreed/disagreed split keeps their
   items apart) may not share a role when either has ``labels_per_item: all``. The
   ``all`` stage reserves a slot for every holder of the role, nobody labels an item twice
   in a workflow, so whoever labels the item in the other stage can never fill their
   reserved slot, and the item would stay in progress forever.

The rules read plain data (nested dicts and lists), so they run both on a finished
``WorkflowConfig`` and on a half-valid document that ``validate_config`` is checking.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from labelflow.engine.config.problems import (
    Problem,
    and_list,
    percent,
    quoted_list,
    sentences,
    stage_where,
    suggestion,
    top_level,
)


class ConfigGraphError(ValueError):
    """Raised by ``WorkflowConfig`` when the stages do not fit together.

    ``problems`` holds every problem found, each with a location. The message lists them.
    """

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(str(problem) for problem in self.problems))


@dataclass(frozen=True)
class _Stage:
    """What the graph rules need to know about one stage."""

    position: int  # place in the list, counting from 1
    name: str | None  # None when missing or not usable
    upstream: str | None  # the stage named by `input`; None for the dataset or a bad input
    outcome: str  # "all", "agreed" or "disagreed"
    labels_per_item: Any
    sample: Any
    clean: bool  # True when the stage has no errors of its own
    roles: tuple[str, ...] = ()  # from `assign_to`; empty when missing or not usable

    @property
    def subject(self) -> str:
        """The stage at the start of a sentence: ``Stage 'x'`` or ``Stage 2``."""
        return f"Stage '{self.name}'" if self.name else f"Stage {self.position}"

    @property
    def target(self) -> str:
        """The stage inside a sentence: ``'x'`` or ``stage 2``."""
        return f"'{self.name}'" if self.name else f"stage {self.position}"

    def takes_everything(self) -> bool:
        return self.sample == 1


def check_graph(data: Mapping[str, Any], bad: Collection[tuple] = ()) -> list[Problem]:
    """Check the graph rules on a config document and return every problem found.

    ``bad`` lists the places that already have errors of their own, as paths such as
    ``("stages", 1, "input")`` (stage numbers count from 0). Rules that would depend on a
    value in one of those places are skipped, so one mistake is reported once.
    The coverage rule runs only when the whole document is clean.
    """
    stages = _read_stages(data, bad)
    problems = _check_unique_names(stages)
    for stage in stages:
        problems += _check_input(stage, stages)
    problems += _check_final(data, stages, bad)
    if not problems and not bad:
        problems += _check_coverage(data, stages)
        problems += _check_parallel_roles(stages)
    return problems


def parallel_stages(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The pairs of stage names that can hold the same item at the same time, in order.

    Two stages are parallel when neither takes its items, directly or through other
    stages, from the other, and no upstream stage splits them: a stage fed by ``x.agreed``
    and one fed by ``x.disagreed`` (or by stages downstream of those) never see the same
    item. For a document that passes the other graph rules.
    """
    pairs = _parallel_pairs(_read_stages(data, ()))
    return [(a.name, b.name) for a, b in pairs if a.name and b.name]


def _has_error(bad: Collection[tuple], path: tuple) -> bool:
    """Whether ``path``, or anything inside it, is known to be wrong."""
    return any(tuple(place[: len(path)]) == path for place in bad)


def _read_stages(data: Mapping[str, Any], bad: Collection[tuple]) -> list[_Stage]:
    """Read the facts the rules need from the document, ignoring anything unusable."""
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list | tuple):
        return []
    stages = []
    for index, entry in enumerate(raw_stages):
        if not isinstance(entry, Mapping):
            stages.append(_Stage(index + 1, None, None, "all", 1, 1.0, clean=False))
            continue
        name = entry.get("name")
        if not isinstance(name, str) or _has_error(bad, ("stages", index, "name")):
            name = None
        upstream, outcome = None, "all"
        raw_input = entry.get("input")
        if isinstance(raw_input, str) and not _has_error(bad, ("stages", index, "input")):
            upstream, _, outcome = raw_input.partition(".")
        roles: tuple[str, ...] = ()
        if not _has_error(bad, ("stages", index, "assign_to")):
            roles = _read_roles(entry.get("assign_to"))
        stages.append(
            _Stage(
                position=index + 1,
                name=name,
                upstream=upstream,
                outcome=outcome or "all",
                labels_per_item=entry.get("labels_per_item", 1),
                sample=entry.get("sample", 1.0),
                clean=not _has_error(bad, ("stages", index)),
                roles=roles,
            )
        )
    return stages


def _read_roles(assign_to: object) -> tuple[str, ...]:
    if isinstance(assign_to, str):
        return (assign_to,)
    if isinstance(assign_to, list | tuple) and all(isinstance(role, str) for role in assign_to):
        return tuple(assign_to)
    return ()


def _check_unique_names(stages: list[_Stage]) -> list[Problem]:
    positions: dict[str, list[int]] = {}
    for stage in stages:
        if stage.name:
            positions.setdefault(stage.name, []).append(stage.position)
    return [
        Problem(
            stage_where(name, places[1], "name"),
            f"Stage names must be unique, but '{name}' is used by stages {and_list(places)}. "
            "Rename all but one of them.",
        )
        for name, places in positions.items()
        if len(places) > 1
    ]


def _check_input(stage: _Stage, stages: list[_Stage]) -> list[Problem]:
    """The stage's input is a stage listed earlier, and the output it names can exist."""
    upstream = stage.upstream
    if upstream is None:
        return []
    where = stage_where(stage.name, stage.position, "input")
    earlier = [s for s in stages[: stage.position - 1] if s.name]
    earlier_names = list(dict.fromkeys(s.name for s in earlier))

    if upstream in earlier_names:
        source = next(s for s in earlier if s.name == upstream)
        return _check_outcome(stage, source, where)
    if upstream == stage.name:
        message = (
            f"{stage.subject} takes its items from itself. "
            "An input must be a stage listed before it."
        )
    elif upstream in {s.name for s in stages[stage.position :]}:
        message = (
            f"{stage.subject} takes its items from '{upstream}', but '{upstream}' is "
            f"listed after {stage.target}. Stages are listed in order, and an input must "
            f"be a stage listed earlier. Move '{upstream}' above {stage.target}."
        )
    elif not earlier_names:
        message = (
            f"{stage.subject} takes its items from '{upstream}', but no stage is listed "
            "before it. To take every item of the dataset, leave `input` out."
        )
    else:
        message = sentences(
            f"{stage.subject} takes its items from '{upstream}', but no stage listed "
            "before it has that name.",
            suggestion(upstream, earlier_names),
            f"The stages listed before it are {quoted_list(earlier_names)}.",
        )
    return [Problem(where, message)]


def _check_outcome(stage: _Stage, upstream: _Stage, where: str) -> list[Problem]:
    """``.agreed`` and ``.disagreed`` need a stage whose items can have several labels."""
    if stage.outcome == "all" or not upstream.clean:
        return []
    if not _is_exactly_one(upstream.labels_per_item):
        return []
    return [
        Problem(
            where,
            f"{stage.subject} takes '{upstream.name}.{stage.outcome}', but '{upstream.name}' "
            "labels each item only once (labels_per_item is 1), so no item can agree or "
            f"disagree and this input would always be empty. Set `labels_per_item` to 2 or "
            f"more on '{upstream.name}', or take its whole output with "
            f"`input: {upstream.name}`.",
        )
    ]


def _is_exactly_one(value: Any) -> bool:
    return type(value) is int and value == 1


def _check_final(data: Mapping[str, Any], stages: list[_Stage], bad: Collection[tuple]) -> list:
    """``final`` is a non-empty list of distinct stage names."""
    final = data.get("final")
    if _has_error(bad, ("final",)) or not isinstance(final, list | tuple):
        return []
    where = top_level("final")
    if not final:
        return [
            Problem(
                where,
                "`final` is an empty list, so no item would get a final label. List at least "
                "one stage, or write `final: none` if this workflow should not produce "
                "final labels.",
            )
        ]
    names = list(dict.fromkeys(s.name for s in stages if s.name))
    problems = []
    seen: set[str] = set()
    for entry in final:
        if not isinstance(entry, str):
            continue
        if entry not in names:
            message = sentences(
                f"`final` lists '{entry}', but there is no stage with that name.",
                suggestion(entry, names),
                f"The stages are {quoted_list(names)}." if names else "",
            )
            problems.append(Problem(where, message))
        elif entry in seen:
            repeat = Problem(
                where, f"`final` lists '{entry}' more than once. List each stage once."
            )
            if repeat not in problems:
                problems.append(repeat)
        seen.add(entry)
    return problems


def _check_coverage(data: Mapping[str, Any], stages: list[_Stage]) -> list[Problem]:
    """Every stage is in ``final`` or passes all its items on, so no item is left unlabeled."""
    final = data.get("final")
    if final == "none":
        return []
    listed = {s.name for s in stages} if final is None else set(final)
    problems = []
    for stage in stages:
        if stage.name in listed:
            continue
        reasons = _why_items_can_be_lost(stage, stages)
        if reasons is None:
            continue
        if reasons:
            cause = f"not all of its items go on to a later stage ({'; '.join(reasons)})"
            consequence = "some items"
        else:
            cause = "no later stage takes its items"
            consequence = "its items"
        problems.append(
            Problem(
                stage_where(stage.name, stage.position),
                f"Stage '{stage.name}' is not listed in `final`, and {cause}, so {consequence} "
                f"would finish with no final label. Add '{stage.name}' to `final`.",
            )
        )
    return problems


def _why_items_can_be_lost(stage: _Stage, stages: list[_Stage]) -> list[str] | None:
    """Why some of the stage's items could go nowhere, or None if all of them go on.

    An empty list means that no later stage takes the stage's items at all.
    """
    takers = {
        outcome: [s for s in stages if s.upstream == stage.name and s.outcome == outcome]
        for outcome in ("all", "agreed", "disagreed")
    }
    if any(taker.takes_everything() for taker in takers["all"]):
        return None
    reasons = [f"{t.name} takes only a {percent(t.sample)} sample" for t in takers["all"]]

    labels = stage.labels_per_item
    if type(labels) is int and labels >= 2:
        lost = [
            _lost_through(stage, outcome, takers[outcome]) for outcome in ("agreed", "disagreed")
        ]
        if not any(lost):
            return None  # both sides go on in full
        if takers["agreed"] or takers["disagreed"]:
            reasons += [reason for reason in lost if reason]
    elif takers["agreed"] or takers["disagreed"]:
        # `all` or `{count, fraction}` may give an item only one label, which has no outcome.
        reasons.append(
            f"some of its items get only one label, so they never reach "
            f"'{stage.name}.agreed' or '{stage.name}.disagreed'"
        )
    return reasons


def _lost_through(stage: _Stage, outcome: str, takers: list[_Stage]) -> str:
    """Say why the agreed (or disagreed) items of a stage do not all go on; empty if they do."""
    if not takers:
        return f"no stage takes '{stage.name}.{outcome}'"
    if any(taker.takes_everything() for taker in takers):
        return ""
    sampled = ", ".join(f"{t.name} takes only a {percent(t.sample)} sample" for t in takers)
    return f"{sampled} of '{stage.name}.{outcome}'"


# ------------------------------------------------------------------- parallel stages


def _paths(stages: list[_Stage]) -> dict[str, set[tuple[str, str]]]:
    """For each stage, the ``(stage, outcome)`` links an item follows to reach it."""
    paths: dict[str, set[tuple[str, str]]] = {}
    for stage in stages:
        if stage.name is None:
            continue
        if stage.upstream is None or stage.upstream not in paths:
            paths[stage.name] = set()
        else:
            paths[stage.name] = paths[stage.upstream] | {(stage.upstream, stage.outcome)}
    return paths


def _parallel_pairs(stages: list[_Stage]) -> list[tuple[_Stage, _Stage]]:
    """The pairs of stages that can hold the same item at the same time (``parallel_stages``)."""
    paths = _paths(stages)
    pairs = []
    for first, second in combinations([s for s in stages if s.name], 2):
        upstream_of_first = {name for name, _ in paths[first.name]}
        upstream_of_second = {name for name, _ in paths[second.name]}
        if first.name in upstream_of_second or second.name in upstream_of_first:
            continue  # one takes its items from the other
        if _split_apart(paths[first.name], paths[second.name]):
            continue  # an item agreed or disagreed upstream, never both
        pairs.append((first, second))
    return pairs


def _split_apart(first: set[tuple[str, str]], second: set[tuple[str, str]]) -> bool:
    """Whether a stage upstream of both sends its agreed items one way and its disagreed another."""
    agreed = {name for name, outcome in first if outcome == "agreed"}
    disagreed = {name for name, outcome in first if outcome == "disagreed"}
    other_agreed = {name for name, outcome in second if outcome == "agreed"}
    other_disagreed = {name for name, outcome in second if outcome == "disagreed"}
    return bool(agreed & other_disagreed) or bool(disagreed & other_agreed)


def _check_parallel_roles(stages: list[_Stage]) -> list[Problem]:
    """Parallel stages sharing a role may not use ``labels_per_item: all`` (rule 6)."""
    problems = []
    for first, second in _parallel_pairs(stages):
        shared = [role for role in first.roles if role in second.roles]
        everyone = [s for s in (first, second) if s.labels_per_item == "all"]
        if not shared or not everyone:
            continue
        the_role = (
            f"the role '{shared[0]}'" if len(shared) == 1 else f"the roles {quoted_list(shared)}"
        )
        if len(everyone) == 2:
            consequence = (
                f"Both have `labels_per_item: all`, which reserves a slot in each stage for "
                f"every holder of {the_role}, but nobody labels an item twice in a workflow, so "
                "each person can fill only one of their two slots, and the item would stay in "
                "progress forever."
            )
        else:
            (reserving,) = everyone
            other = second if reserving is first else first
            consequence = (
                f"'{reserving.name}' has `labels_per_item: all`, which reserves a slot for "
                f"every holder of {the_role}, but nobody labels an item twice in a workflow, so "
                f"anyone who labels the item in '{other.name}' first can never fill their slot "
                f"in '{reserving.name}', and the item would stay in progress forever."
            )
        problems.append(
            Problem(
                stage_where(everyone[0].name, everyone[0].position, "labels_per_item"),
                sentences(
                    f"Stages '{first.name}' and '{second.name}' can both hold the same item "
                    f"at the same time ({_why_parallel(first, second)}), and both are assigned "
                    f"to {the_role}.",
                    consequence,
                    "Give the two stages different roles, or chain them so that one takes "
                    f"the other's output (`input: {first.name}` on '{second.name}').",
                ),
            )
        )
    return problems


def _why_parallel(first: _Stage, second: _Stage) -> str:
    if first.upstream is None and second.upstream is None:
        return "both take every item of the dataset"
    first_input = f"{first.upstream}.{first.outcome}" if first.outcome != "all" else first.upstream
    second_input = (
        f"{second.upstream}.{second.outcome}" if second.outcome != "all" else second.upstream
    )
    if first_input == second_input:
        return f"both take their items from '{first_input}'"
    return "neither takes its items from the other"
