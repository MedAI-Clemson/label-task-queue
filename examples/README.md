# Examples

Everything here is runnable. `quickstart.py` runs two of the workflows below in memory;
the workflow files are validated by the test suite, and their resolved form and diagrams
are kept as snapshots in `tests/config/resolved/` and `tests/config/mermaid/`.

## `quickstart.py`

```bash
python examples/quickstart.py
```

A whole labeling project with no server and no database: roles and people, two datasets,
two workflows started from the YAML files below, simulated labelers, and then the feedback
tools: progress, the sampling report, `explain(item)` for an image that was sampled for
review, the items export, and the score report. The run is seeded, so the output is the
same every time. Read the script top to bottom; it is the Python API in about 200 lines.

## Workflows (`workflows/`)

Each file is a worked example with a comment on every line that makes a decision. Read
them in this order; each adds one idea.

| File | Pattern | What it teaches |
| --- | --- | --- |
| [`review_sample.yaml`](workflows/review_sample.yaml) | Annotators label everything; an expert reviews a 20% sample. | `input`, `sample`, and `final` as an ordered list of sources. |
| [`adjudicate.yaml`](workflows/adjudicate.yaml) | Two reads per image; an expert settles disagreements. | `labels_per_item: 2`, the `.disagreed` output, and the agreed label. |
| [`training_round.yaml`](workflows/training_round.yaml) | Trainees label a gold set and are scored against the key. | `labels_per_item: all`, `score_against`, `final: none`. |
| [`gold_check.yaml`](workflows/gold_check.yaml) | Ordinary labeling with gold images mixed in, plus a blind 10% review. | `score_against` where only some items have a key (`no_key`), and `show_prior: false`. |
| [`spot_check.yaml`](workflows/spot_check.yaml) | 20% of images get three reads to measure agreement; disputes go to an expert. | `labels_per_item: {count, fraction}`. |

The first three are the examples in [`docs/phase1-design-notes.md`](../docs/phase1-design-notes.md).
The complete key reference is in the top-level [README](../README.md#the-config-vocabulary),
and the published JSON Schema is [`schema/workflow-config.schema.json`](../schema/workflow-config.schema.json).

To check a file of your own, and see it with every default filled in plus its diagram:

```python
from labelflow.engine.config import resolved_yaml, to_mermaid, validate_config

report = validate_config(open("my_workflow.yaml").read())
print(report.render())  # every error and warning, in plain language
if report.ok:
    print(resolved_yaml(report.config))
    print(to_mermaid(report.config))
```

## Ontologies (`ontologies/`)

[`chest_xray.yaml`](ontologies/chest_xray.yaml) defines the label task `image_ontology@1`
that every workflow above refers to: the classifications a labeler must fill in, the mask
classes they must draw (one mask per class), and the IoU threshold at which two labelers'
masks count as agreeing. The server loads every `*.yaml` in the folder named by
`LABELFLOW_LABEL_TASKS` (`run.sh` points it here).
