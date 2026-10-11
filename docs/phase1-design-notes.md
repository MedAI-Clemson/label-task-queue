# label-task-queue: Phase 1 Design Notes

Oct 11, 2026 · Hudson Smith · revision 3

## Status of this note

This is a draft for review, not a final spec. It records what was settled in design conversations in October 2026, on top of the earlier project briefing.

Each statement is marked as one of two kinds. **Decided** means Hudson has stated or confirmed it. **Proposed** means it is a suggestion that still needs a yes or no. Questions not yet discussed are collected in the last section.

On naming: what a researcher writes is called a **labeling workflow**, not a router. The program that runs it is called the engine internally; users never need that name. The repository name is unchanged for now.

**What changed in revision 3.** Decisions from the review of the first implementation
(details in [`phase1-implementation-notes.md`](phase1-implementation-notes.md)):

- The proposed output names, `final` defaults, default work order, edit rules and the name
  `show_prior` are confirmed. The package is named `labelflow`.
- The interface has two mask tools, brush and polygon, both producing the same per-class mask. The agreed label for masks stays a copy of the earliest label.
- A labeler picks which workflow to work on.
- Admins can pause and resume a stage or a whole workflow.
- An item that already has its final label can still be excluded; the final label stays in
  the log as history.
- A labeler who releases an item is offered it again only after their other work.

**What changed in revision 2.** These changes were all agreed in review.

- The four policies (`distribute`, `all`, `sample`, `consensus`) are replaced by two independent stage settings: `sample` decides which items enter a stage, and `labels_per_item` decides how many people label each one.
- Assignment is **pull**, not push. The workflow creates slots; labelers claim them.
- Who holds which role is project state, changed through logged admin events. It is no longer written in the workflow config.
- A running workflow can be edited **forward-only**, with a preview of the change. It is no longer frozen.
- An explicit **final step** lists where final labels come from, replacing the "last stage wins" rule.
- Random draws are hashed from the item rather than drawn from a seeded stream. The `per: upstream_labeler` sampling option is dropped.
- Stored state may be kept as derived tables, as long as it can be rebuilt from the log.
- Events reference what the labeler was shown, rather than storing one combined hash.
- An item has one final label per workflow, not one overall.
- Scoring against a key uses the same consensus check.
- Phase 1 masks are one per class, and the agreed label is copied from one submitted label, not merged.
- The interface scope is reduced.
- Added: what labelers and admins can do with a task (claim, submit, release, exclude), the rule that nobody labels the same item twice in a workflow, the default work order, warnings for roles too small to fill a stage, and an implementation plan.

## Phase 1 at a glance

Phase 1 is the smallest system that can run the anchor project, an image ontology task, with full provenance (decided). The same pieces also cover training labelers on gold-standard data, with no extra features.

1. **One authoring format:** a YAML config. Each stage names its input, the roles assigned to it, and two settings. A final step says where final labels come from. The config is the single source of truth, also shown as an automatically drawn diagram.
2. **Two stage settings:** `sample` decides which arriving items enter the stage. `labels_per_item` decides how many different people label each one.
3. **Pull assignment:** admitting an item creates slots, and labelers with the right role claim slots when they ask for work.
4. **An explicit final step:** an ordered list of stages, where the first stage that labeled the item supplies its final label.
5. **Item properties,** such as a gold-standard key.
6. **Several independent workflows** per project. They are linked only by manual, logged role changes.
7. **A label task** that defines the ontology, validates labels, and runs the consensus check. Phase 1 ships one built-in label task driven by an ontology file.
8. **An append-only event log** in which every event records the rule that caused it.
9. **Forward-only edits** to a running workflow, each previewed and logged.
10. **Feedback tools:** `explain(item)`, two export tables, the resolved config with its diagram, and a score report.

Phase 1 also builds a minimal annotation interface for 2D images, and runs self-hosted with accounts created by an admin (decided).

## The anchor project: an image ontology

- Each item is one image (decided). Images may be 2D or 3D depending on the project; phase 1 handles 2D.
- The label for an image follows an ontology that mixes kinds of labels, such as classifications and segmentations (decided).
- In phase 1, masks are one per class (semantic segmentation), so labels never need to match individual objects across labelers (decided).
- Each stage labels the whole ontology for an image (decided). Stages that label only part of it are deferred.

The **unit of routing** is the image: the workflow hands out whole images, never parts of one. The **unit of labeling** is everything the ontology asks for on that image. The workflow knows only about the first; the second belongs to the label task. The design was first worked out on a patient video project, and it fits that project the same way.

## The central emphasis: small workflows, high trust

Phase 1 spends its effort on making small workflows auditable and easy to read, not on machinery for large ones (decided). A labeling workflow is usually two or three stages, and nobody is confused about what runs when. What people cannot see, even in a small workflow, is whether a sample was drawn fairly, whether every disagreement was reviewed, and how each final label came to be.

1. **No clever dependency inference.** Stages are listed in order, and each names the stage it takes items from.
2. **The common case is a few lines.** "Annotators label, an expert reviews a sample" should read almost like that sentence.
3. **Depth is allowed, not optimized.** A ten-stage workflow should work, but it should not drive design decisions.
4. **The effort goes to provenance and feedback:** the event log, the "because" field, and the explain tools.

## Three components

The system has three parts, each with one job (decided).

| Component | What it owns | What it does not know about |
| --- | --- | --- |
| **Labeling workflow** | Stages; which stage feeds which; stage settings; the final step; slots and claims; the event log | What is inside a label |
| **Label task** | The ontology; validating a submitted label; the consensus check; building the agreed label | Who works on what, or in what order |
| **Interface** | Showing the image, with earlier labels where configured, and capturing the label | Routing of any kind |

**Workflow and label task: one yes-or-no question (decided).** The workflow never opens a label. It asks the label task one question about the labels an item has received: is there consensus? The answer is yes or no, and it means the same thing for every label task. How consensus is judged lives entirely in the label task.

**Label task and interface: the consensus detail (decided).** The same check also produces a record of which parts of the label agree. The interface uses it; the workflow never sees it.

**Rejected: outcome names defined by the label task (decided).** Letting a label task return its own outcomes, such as "minor" and "major" disagreement, would couple the workflow config to that task's vocabulary.

**The final label is its own record (decided).** Each item has exactly one final label **per workflow**, separate from every individual label and belonging to no labeler. It records which labels and which rule produced it. Individual labels stay exactly as submitted.

## Mental model: items flow, people claim

Items are the only thing that moves through a workflow (decided). A **stage** is defined by its input, the roles assigned to it, and its two settings. When an item arrives, the stage decides whether it enters (`sample`) and how many labels it needs (`labels_per_item`). Each needed label becomes an open **slot**.

**Assignment is pull (decided).** A labeler asks for work and claims one open slot they are eligible for. Nobody is assigned work in advance. This means:

- a labeler who is away holds nothing up;
- a role change takes effect at the next claim, with no rebalancing;
- an expiring reservation, later, is just a claim returning to the pool.

**Eligibility (decided).** To claim a slot, a person must:

- hold one of the stage's roles at the moment of the claim, and
- not have labeled this item already **anywhere in this workflow**. This prevents self-review. If no eligible person exists, the slot waits, and `explain(item)` says why.

**Default order (decided).** Slots in later stages are offered first, so items finish. Within a stage, the oldest admitted item comes first. Phase 1 has no other ordering.

**Task actions (decided):**

| Who | Action |
| --- | --- |
| Labeler | Claim, submit, release with a reason |
| Admin | Exclude an item, for example an image that cannot be labeled |
| Admin | Cancel the open slots in a stage |
| Admin | Pause or resume a stage, or a whole workflow |

Every action is a logged event. Automatic expiry of claims is deferred.

**Pausing (decided).** A pause stops claims only. No new work is handed out in a paused stage
or workflow, but items keep arriving and their slots wait, and claims already held can still
be submitted or released. A stage's pause and its workflow's pause are independent.

**Releasing (decided).** A person who releases an item is offered it again in that stage only
after every other slot they are eligible for.

**Excluding (decided).** Exclusion is per workflow. It is allowed even after the item's final
label exists; the final label then stays in the log as history, and the item shows as excluded.

**One image's path:**

1. Image 17 is admitted to `annotate`, which needs one label, so one slot opens.
2. Ana asks for work, claims the slot, and labels the image.
3. On leaving `annotate`, image 17 is checked by the downstream stage `expert_review`, which samples 20%. Image 17's draw is 0.07, so it is admitted and a slot opens.
4. The expert claims and labels it.
5. No later stage takes it, so the final step creates its final label. Every step is logged with its cause.

**Items move one by one (decided).** An item leaves a stage as soon as all its slots are filled, and the consensus check has run if there were two or more labels. Admission to every downstream stage is decided **at that moment**. An item is therefore **done** exactly when it has no open slots and no stage admitted it further. The final label is created then, as a logged event.

**Random draws are hashed (decided).** Each draw is `hash(workflow seed, stage, item)`, mapped to a number between 0 and 1, and logged with the event. This makes draws deterministic, independent of the order items arrive in, and recomputable by anyone. A fraction such as 20% holds on average, and the export shows the actual rates, including per labeler.

**Items carry properties (decided),** such as the source dataset or a gold-standard key. A stage reads an item property only through a named setting. In phase 1 the only such setting is `score_against`. Items can be added to a dataset while a workflow runs; each import is logged, and the image is hashed at import.

## Authoring: a config file

A labeling workflow is written as a YAML config, not as a program (decided). Python objects that compile to the same config can come later.

**The config document is the single source of truth (decided).** The web API works with whole documents: validate, preview, start, and propose an edit to a running workflow (see "Changing a running workflow"). There are no endpoints that build a workflow piece by piece. Any future graphical editor would read and write this same document. The diagram is drawn automatically from the config, is read-only, and lives on the admin pages (decided).

It should feel like Snakemake to a Snakemake user: a stage is like a rule, and its input names the stage it takes items from (decided). But it is not executable code. There is no inline code and no expression language, and a published JSON Schema lets an AI assistant write valid configs and lets the validator catch invented keys.

**Example: annotators label, an expert reviews a sample (proposed syntax).**

```yaml
workflow: xray_main
dataset: xray_batch_1
label_task: image_ontology@1

stages:
  - name: annotate
    assign_to: annotator          # each image labeled once (the default)

  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2                   # 20% of annotated images

final: [expert_review, annotate]  # the review label wins where it exists
```

**The whole vocabulary (proposed names):**

| Level | Keys |
| --- | --- |
| Top level | `workflow`, `dataset`, `label_task`, `stages`, `final` |
| Per stage | `name`, `input`, `assign_to`, `sample`, `labels_per_item`, `score_against`, `show_prior` |

## Stage settings

**`sample` decides which items enter (decided).** The default is every arriving item. `sample: 0.2` admits an item when its hashed draw is below 0.2.

**`labels_per_item` decides how many different people label each admitted item (decided).**

- `1` is the default.
- An integer such as `3` means every admitted item gets 3 labels.
- `{count: 3, fraction: 0.2}` means 20% of admitted items get 3 labels and the rest get 1 (the earlier `consensus` design). The 20% is chosen by a hashed draw.
- `all` means everyone who holds the stage's roles **when the item is admitted**. The list is recorded in the log, so a later promotion adds no work on old items.

The settings combine freely. For example, "an expert reviews 20% of images, and each sampled image is read by 2 experts" is `sample: 0.2` plus `labels_per_item: 2`.

**`score_against: <property>` (decided)** compares each label with a key stored on the item, such as a gold-standard answer. The comparison is the label task's consensus check, run on the label and the key. It changes no routing. A labeler's score is the fraction of their items that agreed with the key (proposed).

**`show_prior`** controls what a reviewer starts from (decided). By default, a reviewer sees earlier labels as layers they can toggle, with the agreed and disputed parts flagged. Starting from earlier work nudges reviewers toward accepting it, so the setting is visible in the config and a stage can opt into blind review (`show_prior: false`).

**When a stage's roles are too small (proposed).** If `labels_per_item` exceeds the number of people currently holding the stage's roles, validation warns when the workflow starts and again on each role change.

### Extending later: three extension points (proposed)

More complex sampling or assignment logic fits into three extension points. Each has a small library of named strategies with parameters:

| Extension point | Decides | Phase 1 | Later examples |
| --- | --- | --- | --- |
| **Admission** (`sample`) | Does an arriving item enter? | A random fraction | A minimum per upstream labeler; stratified by an item property; by the upstream labeler's role (`student_new: 1.0, student: 0.1`); top-scoring items from a model (active learning); a stage cap |
| **Allocation** (`labels_per_item`) | How many slots? | Count, fraction, or `all` | Depending on an item property or the upstream labeler's role |
| **Claim** (future `order`, `limit`, eligibility keys) | Which slot goes to whom, and when? | Fixed order; the no-self-review rule | `order: random` or by score; `limit: 40 per week`; extra eligibility rules |

Every strategy follows the same contract:

- It is a **named, parameterized, pure function** at one extension point.
- It reads item properties only through named parameters.
- It returns a decision plus its reason, which becomes the "because" field in the log.

The short form `sample: 0.2` stands for the random-fraction strategy. Custom strategies are Python plugins registered with a deployment, named and versioned, with a hash of their source code recorded in the log. They have the same status as label tasks: the config chooses among them, but never contains code.

Two kinds of logic stay out of scope by design: anything that reads the content of a label, beyond "agreed or not", and anything that links items across workflows.

## Branching on consensus

Escalation is not a built-in feature (decided). It is made of ordinary stages:

- When an item receives two or more labels in a stage, the workflow asks the label task whether they agree.
- A stage's input can be the whole output of an earlier stage, or only the items that agreed or that disagreed.
- The config holds no numbers and no conditions for this split.

**Output names (decided):**

| Input | Contains |
| --- | --- |
| `annotate` | Every item that completed the stage |
| `annotate.agreed` | Items with two or more labels where the labelers agreed |
| `annotate.disagreed` | Items with two or more labels where they did not |

Items labeled only once appear only in the plain output (decided).

```yaml
stages:
  - name: annotate
    assign_to: annotator
    labels_per_item: 2              # every image read by 2 annotators

  - name: adjudicate
    input: annotate.disagreed       # only images where they disagreed
    assign_to: expert

final: [adjudicate, annotate]
```

Images that agree finalize from `annotate` with the agreed label. Images that disagree finalize from `adjudicate`, where the expert submits a complete label, agreed parts included.

**The costs, accepted in review (decided):**

- The routing split is only two ways.
- Different strictness for the same label task means a new version of the label task, not a workflow setting.

## The final step

Every workflow ends in an explicit final step: an ordered list of stages (decided). For each item, **the first listed stage that labeled it** supplies the final label:

- if that stage gave the item one label, that label is used;
- if it gave several, the consensus check's agreed label is used;
- if those labels did not agree, the item is marked **unresolved**, and the export shows it that way.

**Validation (proposed).** Every stage must either be listed in `final` or pass all of its output downstream, through a stage that takes its whole output without sampling, or through stages taking both its agreed and disagreed items. This makes it impossible for an item to finish with no final label. For example, a stage that only *samples* from `annotate` does not pass all of it downstream, so `annotate` must be listed.

**Defaults (decided).** If `final` is omitted, it means every stage, last-listed first, and the resolved config shows it spelled out. A workflow that should produce no final labels, such as a training round, declares `final: none`.

## Changing a running workflow

Projects change course, so a running workflow can be edited, but only **forward-only** (decided).

- **Every edit is a new config version.** An admin proposes it and sees a change preview: the diff, plus counts of the open slots, items in progress and finished items it affects. Activating it is a logged event that carries the diff.
- **A new version governs only decisions made after it is activated.** Admissions, slots, labels and final labels that already exist stay as they are, and each keeps the version it was made under.
- **Edits that apply in place:**
  - changing `sample`, `labels_per_item`, `show_prior` or `score_against`;
  - changing `assign_to`, which applies at the next claim, so it reaches open slots too;
  - adding a stage;
  - changing `final`, which applies to items not yet finalized;
  - removing a stage with no open slots.
- **Edits that need a new workflow:** changing the dataset, the label task, a stage's input, or a stage's name. These change what existing labels mean.
- **Existing open slots stay.** Cancelling them is a separate, explicit, logged admin action.
- **Applying a change to items already past a stage ("backfill") is deferred.** It would be an explicit action, never a side effect of an edit. Hashed draws make it clean: raising a sample from 0.2 to 0.3 admits exactly the items whose draws lie between 0.2 and 0.3, so the combined sample is still a uniform 30%.

## Several workflows, roles, and keys

- **Several workflows (decided).** A project can run several independent workflows, each over a dataset. Workflows do not pass items to each other.
- **Roles are project state, changed by hand (decided).** Roles are defined once per project and shared by all its workflows. An admin assigns a role to a person or removes it, and each change is one event in the log. The tool never changes a role by itself. A workflow config only names the roles it uses.

### Example: training labelers on gold-standard data

Nothing here is a training feature. "Trainee" is just a role name the project chose.

1. Workflow one runs over a small gold-standard set. Each image carries the expert's answer as `gold_key`. Everyone with the trainee role labels every image, and each label is scored against the key.
2. The score report shows each trainee's agreement with the key.
3. A review session with the trainees happens outside the tool.
4. Workflow two runs over a second gold set, with the same shape.
5. An admin changes the role of the people who are ready, from `trainee` to `annotator`. That logged change is what makes them eligible for the main workflow.

```yaml
workflow: training_round_1
dataset: gold_set_1              # each image carries a gold_key property
label_task: image_ontology@1

stages:
  - name: gold_read
    assign_to: trainee
    labels_per_item: all         # every trainee labels every image
    score_against: gold_key

final: none                      # training produces scores, not final labels
```

The same pieces support other designs, such as mixing a few gold images into ordinary labeling as a quality check.

## The label task component

The label task holds everything about what a label is (decided). Label tasks are **code**, the only code in the system. They are named and versioned, registered with a deployment, and referenced from the config, for example `image_ontology@1`.

Phase 1 ships **one built-in label task, driven by an ontology file**, so most projects write no Python (decided).

- **The ontology file** lists:
  - classifications: a name, the allowed values, and whether each is required;
  - mask classes, one mask per class;
  - the agreement threshold for each mask class.
  
  The ontology version is recorded with every label.
- **Validation on submit.** A label that does not match the ontology is rejected with a plain-language message.
- **The consensus check,** for any two or more labels:
  - classifications agree when they match exactly;
  - masks agree when their pairwise IoU (intersection over union) meets the class threshold, and two empty masks agree;
  - it returns yes or no; the agreed label, when yes, which is a **copy of the earliest submitted label**, not a merge; and per-field agree/disagree flags, with IoU values, for the interface.
  
  The rule version is logged with every answer.
- **Scoring against a key** runs the same check on the label and the key (decided). A stricter comparison would be a new label task version.
- **Mask encoding (proposed):** run-length encoded inside the label's JSON, in the COCO RLE style.

## Provenance and feedback

Provenance is the center of gravity for phase 1, built in from the first commit (decided).

**The event log is the source of truth (decided).** The log is append-only. Derived tables, such as current slots, labels and final labels, are updated in the same transaction as each event, and they can always be rebuilt from the log. A test proves that rebuilding gives identical state. Each event records:

- who, when, and which stage;
- the version of the workflow config in force;
- the rule that caused it (the "because" field);
- for a random decision, the draw value and the threshold it was compared to;
- **references to what the labeler was shown:**
  - the image's content hash, taken at import;
  - the IDs of any earlier labels shown;
  - the `show_prior` setting;
  - the version of the consensus rule behind any flags;
- the ontology version;
- time from claim to submit, as reported by the interface.

Role changes, workflow version activations and admin actions are events in the same log. Every label can therefore be traced to the role its author held and the config version in force when it was made.

**Feedback tools in phase 1 (decided):**

- `explain(item)`: why the item went where it went, and why it is waiting, if it is.
- **Two export tables:** one row per item (the final label, how it was decided, and its status) and one row per label (the labeler, the role they held, the stage, the version, and the score against the key where set). The **score report** is a summary of the labels table.
- `show_config(resolved=True)`: the full workflow with every default filled in, plus the diagram.
- The **change preview** for edits to a running workflow.

**How dry runs work under pull (proposed, deferred beyond phase 1).** Pull does not get in the way of dry runs. The slots are fixed by `sample` and `labels_per_item`, not by who claims them, so there are two levels:

1. **Expected workload.** Computed directly from the config and the number of items: the expected number of slots per stage and per role, given an assumed disagreement rate, or the rate observed so far in the running workflow. No simulation is needed. Phase 1's change preview is a counts-only version of this.
2. **Timeline.** The real engine runs on an in-memory store with simulated labelers, each with a declared throughput and availability, and a stand-in label task that agrees with a set probability, using a fixed seed. Under pull, each simulated labeler simply claims a slot whenever it is free. The result is printed together with its assumptions. Phase 1's tests with simulated labelers are the start of this.

## The interface

Phase 1 builds a minimal annotation interface rather than plugging into an existing tool (decided). Scope (decided):

- 2D images only.
- Classifications, plus two mask tools (decided): a brush (paint and erase, for irregular regions) and a polygon (click the outline, for crisp boundaries). Both edit the same one-mask-per-class label, so a labeler can outline with the polygon and touch up with the brush. Built on an open-source drawing library.
- For reviewers: earlier labels as toggleable layers, plus the per-field agree/disagree flags.
- Reporting exactly what was shown, as references, for the event log.

3D projects come later, through a connector to a medical viewer such as 3D Slicer or OHIF. Because the interface is a separate component, connectors to other tools stay possible.

## Implementation

**Start fresh (decided).** Phase 1 is a new codebase built around the event log from the first commit, reusing ingredients of the old one: FastAPI patterns, test fixtures, and the Docker setup. The old model's foundations conflict with the design: steps created one at a time through the API, a task table edited in place, and records excluded from every other step once assigned. The current prototype is tagged before removal, so it stays in the history.

**Stack (decided):**

- FastAPI for the web layer.
- Pydantic for the workflow config, the domain objects and the API. It also generates the published JSON Schema for configs.
- SQLAlchemy 2 for storage, replacing SQLModel, and Alembic for database changes.
- SQLite by default; Postgres later, if a deployment needs many simultaneous labelers.

**Structure and safeguards (proposed):**

- The engine (stage logic, extension-point strategies, the event model, explain) is a plain Python module that knows nothing about FastAPI or the database. It reaches storage through a small interface, so tests and dry runs can use an in-memory store.
- Every claim runs in a transaction that takes the write lock first (`BEGIN IMMEDIATE` on SQLite). Database uniqueness constraints back this up: one claim per slot, and one label per person per item per workflow.
- A database trigger refuses any change or deletion of past events. Each event also stores a fingerprint of the one before it, so tampering can be detected.
- The inputs to strategies are immutable Pydantic objects, which backs up the rule that strategies are pure.
- Alembic manages schema changes from the first deployed version.

**Deployment and login (decided):**

- The system is self-hosted. It never assumes where images live or that they can be sent elsewhere; items reference images by path or URL.
- An admin creates every account; there is no self-signup. Identity sits behind one small layer that gives the engine a stable user ID, so other login methods can be added per deployment later.

## Implementation plan

The order follows the design's priorities: the pure engine and provenance first, then storage, the API, and the interface last. Each milestone ends with tests passing and a short demo. The interface is the riskiest piece, so a short drawing-library spike runs early, alongside the engine work.

| # | Milestone | Contents | Exit criteria |
| --- | --- | --- | --- |
| 0 | **Scaffold** | Tag the prototype (`v0-prototype`), then remove it. New package layout: `engine/`, `labeltask/`, `store/`, `api/`, `ui/`. A `pyproject.toml` with pinned dependencies, ruff, pytest, and CI on GitHub Actions. | CI is green on an empty skeleton. |
| 0b | **Interface spike** (in parallel, 1–2 days) | Try 2–3 open-source drawing libraries for brush and polygon masks, editing one mask per class, plus RLE export. Pick one. | A decision note with a working prototype page. |
| 1 | **Config** | Pydantic models for the config; JSON Schema export; the validation rules (inputs refer to earlier stages; output names; the final step's coverage rule; warnings when roles are too small); the resolved view; a Mermaid diagram. | Example configs validate. Each bad config gives a plain-language error, checked by snapshot tests. The schema is published. |
| 2 | **Engine core** | Events and state changes: import, admission (hashed draws), slots, claim (eligibility, order), submit, release, exclude, cancel slots, consensus, finalize, role events. An in-memory store and `explain(item)`. A stand-in label task. | Property tests (using Hypothesis): rebuilding state from the log equals the state built step by step; draws are deterministic and rise with the fraction; nobody labels an item twice in a workflow; no slot is ever claimed twice. Simulated-labeler tests run both example workflows to completion. |
| 3 | **Built-in label task** | The ontology file format, validation, the consensus check (exact match, IoU), the agreed label, the per-field flags, scoring against a key, the RLE mask encoding, versioning. | Unit tests with handmade masks, including the edge cases (empty masks, a missing class). |
| 4 | **Storage** | SQLAlchemy models: the events table plus derived tables; Alembic; the trigger and the fingerprint chain; claims under `BEGIN IMMEDIATE`; the uniqueness constraints; a rebuild command. | The milestone 2 tests also pass against SQLite. A concurrency test of two claimants racing for one slot passes. A test proves the trigger blocks updates and deletes. |
| 5 | **Versioning and edits** | Config versions; the rules for what can be edited in place; the change preview (diff plus counts); activation events. | Tests for each allowed and each refused edit. The preview counts are correct for the example workflows. |
| 6 | **API** | Accounts (admin-created) and login behind the identity layer; projects; role events; dataset and item import (references plus content hash); workflows (validate, preview, start, edit, change preview); labeler endpoints (next, claim, submit, release); admin endpoints (exclude, cancel slots); explain; the two exports as CSV and Parquet. | Every endpoint is tested, including the error paths. The OpenAPI docs are reviewed for plain wording. |
| 7 | **Interface** | Server-rendered pages (Jinja2 + HTMX) plus the chosen drawing library. Labeler page: claim, view, classify and mask, submit or release. Review mode: earlier-label layers and flags. Admin pages: accounts and roles, workflow upload with validation and diagram, progress, exports. | A labeler can complete the review example from start to finish in a browser. What was shown is reported correctly, as checked against the events. |
| 8 | **Pilot** | Run the anchor project on a small real dataset: one main workflow plus one training round. | The audit checklist below passes, and Hudson signs off. |

**Audit checklist for phase 1 (proposed).** Each question must be answerable from the exports or `explain(item)` alone:

- For any final label: which labels produced it, who made them, the roles they held then, the stage, the config version, and the final-step rule used.
- For any sampling decision: the draw, the threshold, and the version.
- For any label: what the labeler was shown.
- For any item still waiting: why.
- For any role change or config edit: who made it, when, and what changed.
- Rebuilding from the log reproduces the current state exactly.

## Deferred

**Out of the design entirely (decided):**

- Inline code or an expression language inside the workflow config.
- Machinery aimed at large graphs, such as automatic dependency inference.
- Anything the tool computes and then acts on by itself, such as a trust score or automatic promotion.

**Later phases (proposed):**

- Python-object authoring, and a graphical editor that edits the same config document.
- More strategies at the three extension points, such as per-labeler minimums, stratified or model-scored sampling, ordering, weekly limits, and custom plugins.
- Backfill after an edit, and automatic expiry of claims.
- Final-step rules beyond "first listed source".
- Matching individual objects across labels, and merging masks.
- Stages that label only part of the ontology.
- 3D images, through a connector to a medical viewer.
- Connectors to existing annotation tools such as CVAT.
- Single sign-on and other login methods, per deployment.
- Postgres, for deployments with many simultaneous labelers.
- Dry runs (the workload and timeline levels above), a generated methods paragraph, `explain(user)`, and `why_waiting(item)`.
- Active learning and model scores.
- A replay study on multi-reader datasets.

## Open questions before implementation

- [x] **Proposed items.** The output names `.agreed` and `.disagreed`, the `final` defaults and `final: none`, the default work order, and the details of what can be edited in place are confirmed. Other statements still marked proposed need a yes or no.
- [x] **Remaining names.** `show_prior` stays; the package is `labelflow`.
- [x] **Mask tool.** Both brush and polygon. The spike in milestone 0b chooses a drawing library that supports both.
- [x] **Agreed label for masks.** A copy of the earliest label is acceptable for the anchor project.
- [x] **Choosing between workflows.** The labeler picks one in the interface.

Outside this note, the briefing's research steps are still to do: a catalog of 10 to 15 real past routing policies, short interviews with former collaborators, and a proper literature check.
