# label-task-queue: Project Briefing

*Prepared 2026-10-10 as a handoff for a conversation partner. It summarizes the project, the research so far, the design ideas under discussion, and the open questions. In this document, "the owner" means the person who started the project.*

*Note: the Phase 1 design has since moved on. See `docs/phase1-design-notes.md` for the current design; this briefing records how the project got there.*

---

## 1. One-paragraph summary

label-task-queue is a **routing engine for human data labeling**. It decides *which item each labeler works on next, how many times each item is labeled, and by whom*, according to a policy the researcher declares. It is meant to sit alongside or behind labeling interfaces, not replace them. The motivation is that real labeling projects, especially academic ones, are shaped by **external constraints** (who the experts are, how much time they have, how much student labelers can be trusted, what phase the project is in), and existing tools force those workflows into a fixed menu of options. The goal is a **simple, composable, highly auditable** way to declare and run a labeling workflow. The intended users are data scientists and researchers, not software engineers.

---

## 2. Origin and motivation (in the owner's words, paraphrased)

- The owner supported many data science projects as a consultant, in healthcare and other fields, with image, text and tabular data. Nearly every project had a labeling workflow.
- Those workflows were governed less by model-performance targets than by **practical constraints**:
  - *Expert scarcity:* who the experts are, how much time they have, and how to allocate that time.
  - *Mixed-skill labor:* trainable student workers with lower accuracy, whose work experts need to see.
  - *Project phase:* "front-load new labels because we're trying to get a model off the ground", and later shift toward quality.
- The owner used **Labelbox** and repeatedly found it hard to implement the routing policies they wanted out of the box. The workflow had to be bent to fit the tool.
- The idea: build a **generic, composable workflow/routing tool** so the policy matches the project, possibly with active-learning support. Deliberately *not* trying to solve everything Labelbox solves (interfaces, data hosting, etc.); focus on **routing**.
- Open uncertainty from the owner: "If it's just for me, it's definitely great for me, but I don't know if others experience this pain point."

---

## 3. Current state of the code

**Repository:** `MedAI-Clemson/label-task-queue`. `main` was at commit `d200ed3` when this briefing was written (October 2026).

**History:** written in August 2023 (17 commits), abandoned mid-refactor. In October 2026 it was modernized and merged to `main`:
- **Stack:** Python 3.12, FastAPI 0.143, Pydantic 2.14, SQLModel 0.0.48, SQLAlchemy 2.1, SQLite. Versions are pinned in `requirements.txt` and `requirements-dev.txt`.
- **Tests:** 121 pytest tests in `tests/`, all passing with warnings treated as errors.
- **Bugs fixed:** queue-step creation crash, globally-unique step rank, PATCH returning 500 on missing IDs, step capacity not enforced, crashing deletes, and others.
- **Run it** with `run.sh` / `test.sh` / Docker. The README explains setup.

**Current data model:**
- **Dataset → Records.** Each record is an arbitrary JSON payload.
- **User.** Has a role: Labeler or Admin.
- **LabelQueue.** One dataset, plus registered users, plus ordered **QueueSteps**.
- **QueueStep.** Has a policy type:
  - `distribute`: one label per record, in sequential or random order. **Implemented.**
  - `consensus`: **stub.**
  - `priority`: **stub.**
  - Also has `num_records` (capacity) and `rank` (order).
- **Task.** One record assigned to one user under one step. Has a `completed_data` field for the label.
- **Endpoint:** `POST /labelqueues/{id}/{user_id}/task/` returns the next assignment. The API also has create, read, update and delete endpoints for every entity.

**Known functional gaps (deliberately not addressed yet):**
- No endpoint to submit or complete a task. Steps never complete, so queues never advance to the next step.
- Records assigned in any step are excluded from all other steps, so a later step can't re-route records from an earlier one. This is a design flaw, not just a bug.
- No authentication: the user ID is passed in the URL. No event log or provenance, no database migrations, no interface.

**Assessment:** a clean, tested starting point. But the current "ordered steps with three policy types" model is **too rigid for the design direction below** and will need to be extended: roles, inputs between stages, per-role sampling and limits, an event log.

---

## 4. Research so far

### 4.1 Survey of similar open-source tools

No actively maintained open-source project is a **UI-independent, composable routing/assignment engine**. Many tools have one or two of the pieces, built into a monolithic app:

| Project | License / status | Relevant features | Why it isn't the answer |
|---|---|---|---|
| **Label Studio Community Edition** | Apache-2.0, very active | Overlap %, task locks that expire, uncertainty sampling, REST API | Per-annotator assignment and review workflows are paid Enterprise features; tied to its own interface |
| **Potato** | GPL-3.0, very active | Richest set of assignment strategies (random, least-annotated, disagreement-driven, active learning), phases, adjudication, agreement metrics | Monolithic app aimed at NLP and survey studies; GPL; no medical imaging |
| **MONAI Label** | Apache-2.0; commits active, last release 2023 | Medical-imaging "give me the next sample" API with pluggable strategies; works with 3D Slicer and OHIF | Single annotator plus AI; no per-user assignment, consensus or stages. **Most natural partner** if contributing upstream |
| **CVAT** | MIT, very active | Job stages (annotate → validate → accept), consensus replicas | Not headless; consensus is 2D only |
| **INCEpTION** | Apache-2.0, active | Annotate → curate state machine, agreement metrics | Text only; monolithic Java app |
| **Zooniverse Panoptes + Caesar** | Apache-2.0, active | Best *architectural* match: a routing API plus a separate rules engine (extract → reduce → rule → effect) | Heavy Rails stack; built for citizen science |
| **PyBossa** | AGPL-3.0, stale since 2024 | Headless task router: N answers per task, priority, expiring locks | Unmaintained |
| Argilla, Doccano | Apache-2.0 / MIT | Basic overlap and assignment | NLP-focused; limited routing |

**Commercial reference points:** Labelbox Workflows, Encord, RedBrick AI and Label Studio Enterprise all sell configurable review and consensus stages, usually as paid features. Prodigy offers custom "task routers". This suggests customers do want routing control.

**Design ideas worth borrowing from these tools:**
- reservations that expire when a task is abandoned;
- per-item redundancy targets, and overlapping only a percentage of items;
- disagreement-driven escalation;
- "least-annotated first";
- work stealing (idle labelers take others' pending items);
- off-the-shelf libraries for combining labels (Crowd-Kit, cleanlab) instead of hand-written majority voting.

### 4.2 Research literature (cited from memory, not yet verified)

There is a substantial machine-learning literature on **allocating labeling budgets**:
- relabeling vs. labeling new items: Sheng, Provost & Ipeirotis 2008, "Get another label?"; Khetan, Lipton & Anandkumar 2018;
- active learning with relabeling: Lin, Mausam & Weld 2016;
- budget allocation: Chen, Lin & Zhou 2013;
- online task assignment: Ho & Vaughan 2012;
- combining labels from multiple labelers: Dawid & Skene 1979; Raykar et al. 2010.

Medical work on multiple readers (e.g., Tanno et al. 2019) mostly asks how to *learn from* multi-reader labels, not how to *allocate* expert effort. Most of the allocation literature assumes cheap, anonymous, interchangeable crowd workers. **This needs a proper literature check.**

### 4.3 How the framing evolved (important)

1. **First framing (assistant's, now set aside): routing as optimization.** "Which policy maximizes label or model quality per expert-hour?" Under that framing, the software gap looked modest. The research value was an allocation study, using the tool as an instrument, possibly validated by replaying existing multi-reader datasets such as LIDC-IDRI.
2. **The owner's correction: routing as governance under constraints.** In academic projects, workflow structure is dictated by *who is available, how much time they have, how much each group is trusted, and what phase the project is in*, not by one quality metric. Under this framing:
   - The value is **expressiveness**: letting the project run the way it has to, instead of the way the tool allows.
   - Building a thin engine beats contributing to Potato and similar tools. Adding a strategy to one monolithic tool doesn't give you composability across tools.
   - A possible publication is a **systems/design paper**: a taxonomy of real labeling policies, plus an executable system, plus case studies. Candidate venues: CHI, CSCW, JAMIA, or ML datasets/MLOps tracks.
   - Replay simulation becomes a **"preview" feature** (estimating expert-hours and coverage before launching) rather than the main research contribution.

---

## 5. Design concepts (current direction)

### 5.1 Core principles

1. **Explicit declarations, not inferred attributes.** The owner rejected abstractions like "trust score" or "measured accuracy" that the tool would compute and act on. They force users to learn new vocabulary *and* guess how the tool maps it to behavior. Instead: **users create roles, assign roles to people, and declare per stage how work goes to each role.** Example: a student "graduates" when an admin moves them from role `student_new` to `student`. That's an explicit, logged human decision. The tool may *display* agreement statistics to inform that decision, but never acts on them by itself.
2. **Every decision traces to one line of config.** "Why did Ana get this item?" should always be answerable by pointing at a rule.
3. **A simple default, with complexity revealed gradually.** The default is a standard linear policy. More capability comes from adding keys. A config written at an earlier level stays valid at every later level.
4. **Very high auditability.** Researchers must be able to trace the provenance of every label in detail and feel confident in what they built.
5. **Easy for non-engineers.** Researcher vocabulary, a small set of concepts, clear feedback, readable code and readable logs.
6. **Readability over writability.** Users will often be helped by AI assistants that write the config. The human still has to *read*, review and trust it.

### 5.2 The building blocks (draft vocabulary, about ten terms)

| Term | Meaning |
|---|---|
| **role** | A user-defined label for a group of people (e.g. `expert`, `student`). Users are assigned roles. |
| **stage** | One step of the workflow. Says who works on it and which items it receives. |
| **input** | Which items a stage receives: all items (default) or the output of another stage. |
| **assign_to** | Which role(s) work on a stage. |
| **labels_per_item** | How many *different* people label each item in this stage (redundancy/consensus). |
| **sample** | The fraction of input items routed to this stage. Can be set per role of the upstream labeler (e.g. 100% of new students' items, 10% of experienced students'). |
| **when** | A condition on the item, e.g. `labels_disagree`. Only a few literal built-ins, plus named Python functions as an escape hatch. |
| **order** | The order items are served in (e.g. `newest_first`, random, or by an external score for active learning). |
| **limit** | A cap, e.g. `40 per week` for an expert. |
| **final** | How the authoritative label is chosen (e.g. last stage wins, majority vote, expert overrides). Must be explicit, with a visible default. |

### 5.3 Levels of complexity, revealed gradually

**Level 0 (default):** one stage, everyone labels, one label per item, served in order.
```yaml
stages:
  - name: label
```

**Level 1:** roles and redundancy.
```yaml
roles:
  expert:  [dr_smith]
  student: [ana, ben, cai]
stages:
  - name: first_read
    assign_to: student
    labels_per_item: 1
```

**Level 2:** stages feeding stages, sampling, conditions, limits. This level covers the owner's motivating cases.
```yaml
roles:
  expert:      [dr_smith]
  student_new: [cai]
  student:     [ana, ben]

stages:
  - name: first_read
    assign_to: [student, student_new]
    order: newest_first            # front-load new items to bootstrap a model

  - name: expert_check
    input: first_read
    assign_to: expert
    sample:                        # per-role sampling of upstream work
      student_new: 100%
      student: 10%
    limit: 40 per week             # protect the expert's time

  - name: adjudicate
    input: double_read
    when: labels_disagree
    assign_to: expert

final: last_stage
```
*(Sketch only: the `double_read` stage that `adjudicate` takes as input isn't shown.)*

**Level 3:** escape hatches written as code, not as a new config language. For example `when: myproject.rules:dice_below_0_8` or a custom `order:` function. Needed because "disagreement" means different things for a category label and for a segmentation mask. To keep this auditable, these must be **named, pure functions** (same inputs, same output), and the log records a **hash of their source code**.

### 5.4 Auditability: provenance design

- An **append-only event log is the source of truth.** Current labels are calculated from it and never edited in place.
- Each event records:
  - who, when and which stage;
  - the **policy version** in force;
  - **the rule that caused it** (the "because" field);
  - a **hash of the record contents** (exactly what the labeler saw);
  - the label schema version;
  - time spent.
- **Randomness is reproducible and logged:** seeded sampling, with the actual random draw recorded.
- Example trace for one item:
```
item 1042  scan_0042.dcm  (sha256 ab12…)          policy v3
11-03 09:14  assigned   → ana (student)      stage first_read
             because    first_read.assign_to = student
11-03 09:16  labeled    ana: "effusion"      (2m 10s)
11-03 09:16  sampled    into expert_check
             because    sample.student = 10%   (draw 0.07, seed 1234)
11-04 14:02  assigned   → dr_smith (expert)  stage expert_check
11-04 14:03  labeled    dr_smith: "no effusion"
11-04 14:03  final      "no effusion"
             because    final = last_stage
```
- **Exports for pandas:** one row per item, with the final label, how it was decided, and every contributing label.
- **Generated "methods paragraph":** the tool writes a description of the labeling protocol from the config plus actual counts, ready for a paper's methods section. For example: "Each image was read once by one of three trained student annotators; a 10% random sample (n=214) was reviewed by a board-certified radiologist, who overturned 18%…"
- **Provenance has to be built in from day one.** It is nearly impossible to add after labels already exist.

### 5.5 Clear feedback for users

- `plan()` / dry run: a flow diagram plus a plain summary *before* launching or changing a policy. For example: "Dr. Smith ≈ 40 items/week; students clear the backlog in about 3 weeks."
- `explain(item)`, `explain(user)`, `why_waiting(item)`.
- `show_config(resolved=True)`: the full policy with all defaults filled in, so nothing is invisible.
- Plain-language validation errors with suggestions ("Did you mean `first_read`?").
- A published schema for the config, so AI assistants produce valid configs and the validator catches invented keys.
- A small routing engine made of pure functions, readable in one sitting. Tests double as worked examples of policies.
- Config written either as Python objects or as YAML. Both compile to the same validated, versioned config file, which is what gets logged.

### 5.6 Analogies from other workflow and scheduling tools

- **GitHub Actions runner labels:** jobs declare `runs-on: <label>` and runners carry labels. This matches roles directly (labelers as runners, roles as labels). Jobs declare `needs:` to wait on other jobs, like stages taking inputs. *Warning:* GitHub's `if: ${{ }}` string expressions became a hard-to-debug mini language. **Don't build a config language.**
- **Kubernetes scheduling:** the model for revealing complexity gradually (`nodeSelector` → affinity → taints and quotas). Anti-affinity corresponds to "k *different* labelers". It is also built on **desired state and reconciliation**: declare what each item should end up with, and the engine works out the next task.
- **CODEOWNERS plus branch protection:** "2 approvals, one from @radiology-team." Proof that role-based review rules can be understood instantly.
- **Terraform plan/apply:** preview before changing anything. This is the model for `plan()`.
- **dbt and Snakemake:** brought engineering discipline (version control, lineage graphs, dry runs, generated documentation) to analysts and researchers with very few new concepts. **This is the closest analogy for the target audience and the trust model.**
- **Warnings:**
  - **BPMN** (an industry standard for drawing business processes) assigns tasks to roles correctly, but is too heavy for most people.
  - **Zendesk-style triggers** are a global, ordered list of rules, and become impossible to reason about once they interact. Keep rules **local to each stage**.

### 5.7 Positioning and integration

- Leaning toward a **thin routing engine** that is independent of any interface.
- An attractive option is a **"routing brain on top of existing tools"**: push work into Label Studio, CVAT, Labelbox, or MONAI Label with OHIF, through their APIs, and pull the labels back. This avoids building an interface and lowers the cost of adoption. Whether each tool's API allows it varies; Label Studio's community edition, for example, gates per-user assignment. **This needs verifying.**
- A minimal built-in interface for simple form-based labeling (classification, free text) could cover projects that don't have an existing tool.

---

## 6. Earlier engineering roadmap (written before the reframing; still partly relevant)

A planning pass produced a phased roadmap *before* the governance reframing. Items that likely still apply in some form:
- **Task lifecycle:** assigned / submitted / skipped / expired; an idempotent "get next" that returns the caller's existing open task first; submit, skip and release endpoints.
- **Reservations that expire,** cleaned up lazily on each request.
- **Concurrency safety:** the database must enforce "k different labelers" and capacity limits. Use conditional updates and row locking (`FOR UPDATE SKIP LOCKED` on Postgres). SQLite allows only one writer at a time.
- **A per-stage work table** (one row per stage and item, with counts of active and finished labels) so stages can route records to each other. It replaces the current exclude-everything-already-assigned logic.
- Postgres plus Alembic migrations; SQLite only for tests and development.
- Authentication, with the user taken from a login token, not the URL. University single sign-on (OIDC) if available.
- A label schema per stage (JSON Schema), validated on submit, with the schema version recorded on each label.
- A stable `external_id` per record; media stored by reference (path, S3, DICOMweb), never inline.
- Progress and agreement reporting; export.
- **Patient data (PHI):** require de-identified data, or deploy on-premises; no SQL logging of record data; pseudonymized exports.

**Updated priority:** the **event log, policy versioning and "because" provenance** move to the very front, ahead of any interface or advanced policies.

---

## 7. Open questions

**Validation and positioning**
1. **Do others share this pain?** The owner's consulting experience across many projects is real evidence. It needs confirming through a policy catalog and short interviews with former collaborators.
2. **Standalone tool, routing brain over existing tools, or both?** Which front ends come first? Which APIs actually allow external assignment?
3. **Contribute or build?** Current leaning: build a thin engine. Borrow ideas from Potato and Caesar. Consider MONAI Label as a partner or upstream target for medical imaging.
4. **Research contribution and venue:** a design/systems paper (taxonomy plus system plus case studies) vs. an empirical allocation study vs. both.

**Workflow semantics**

5. **Do items flow one at a time or in batches?** Does a downstream stage start on an item as soon as the upstream stage finishes *that item* (pipelined, like CI `needs:`), or only after the whole upstream stage is done? Current leaning: pipelined by default.
6. **The default for `final`,** and how to make it visible.
7. **Which built-in `when` conditions,** and how literally each is defined (e.g. what "disagree" means per label type).
8. **Blinding:** do downstream labelers see upstream labels? (e.g. an adjudicator sees them, a second reader doesn't.)
9. **What `limit` is measured in:** items or time? Per week or per project? How is time estimated?
10. **Changing the policy mid-project:** what happens to in-flight tasks and already-sampled items? Versioning plus a `plan()` comparison before applying.
11. **Role changes mid-project:** do they affect only future routing, or pending sampling decisions too?
12. **Active learning:** how model scores get in (upload scores, a callback, or a plugin), and whether retraining stays entirely outside the tool.
13. **Skips, expiry and reassignment** of abandoned work.

**Interface and implementation**

14. **Authoring format:** Python objects, YAML, or both compiled to one canonical config?
15. **Final vocabulary and naming:** keep to about ten researcher-friendly terms.
16. **How generic across data types** (image, text, tabular, DICOM), and how media is referenced and served.
17. **Deployment and data governance:** on-premises vs. hosted, authentication model, PHI handling.
18. **How much of the existing code to keep:** extend the current models (steps, ranks, queue users) or restructure around stages, roles and the event log.

---

## 8. Suggested next steps (none started)

1. **Write a policy catalog:** 10–15 real routing policies from the owner's past projects. For each: the constraint that drove it, what was actually done, and where Labelbox fell short. This is the requirements spec, the test of the vocabulary, and the motivation section of any paper.
2. **Check that the vocabulary in 5.2 can express every catalog entry,** and revise it where it can't.
3. **Show the catalog and vocabulary to 3–5 former collaborators** to test whether others share the pain.
4. **Write a design note in the repo** (principles, building blocks, levels, provenance) as the reference for Phase 1.
5. **Run a focused literature check:** allocating expert annotation effort (especially in medicine), and annotation-workflow systems papers.
6. **Optionally, a replay study** on multi-reader datasets (e.g. LIDC-IDRI) to build and validate the `plan()` preview.

---

## 9. Glossary

- **Routing / assignment:** deciding which labeler gets which item next.
- **Redundancy / consensus:** labeling the same item by several *different* people.
- **Adjudication:** an expert resolves items where labelers disagreed.
- **Provenance:** the complete, traceable history of how a label came to be.
- **Headless / UI-agnostic:** works without its own interface; other tools drive it through an API.
- **Active learning:** ordering items by a model's uncertainty or usefulness so labels help the model most.
- **Revealing complexity gradually:** simple by default, with more capability available as the user needs it.
