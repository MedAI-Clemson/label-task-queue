# label-task-queue: Phase 1 Implementation Notes

Oct 11, 2026 · companion to [`phase1-design-notes.md`](phase1-design-notes.md)

This note records where the implementation stands against the design notes' plan, and
every decision the implementation had to make that the design notes leave open. As in the
design notes, **Decided** means Hudson confirmed it (review of Oct 11, 2026), and **Proposed**
means it is what the code does now and still needs a yes or no. Changing a proposed one is
usually a small, local change.

## Status by milestone

| # | Milestone | Status | Where |
| --- | --- | --- | --- |
| 0 | Scaffold | Done, except the `v0-prototype` tag (see below) | `pyproject.toml`, `.github/workflows/ci.yml` |
| 0b | Interface spike | Done: plain Canvas 2D plus an owned mask module (no library rasterizes to the pixel grid); see [`interface-spike.md`](interface-spike.md) | `spikes/mask-tools/` |
| 1 | Config | Done | `src/labelflow/engine/config/`, `schema/`, `examples/workflows/` |
| 2 | Engine core | Done | `src/labelflow/engine/` |
| 3 | Built-in label task | Done | `src/labelflow/labeltask/`, `examples/ontologies/` |
| 4 | Storage | Done (SQLite only) | `src/labelflow/store/`, `alembic.ini` |
| 5 | Versioning and edits | Done | `src/labelflow/engine/edits.py` |
| 6 | API | Done (Docker image not built in CI) | `src/labelflow/api/` |
| 7 | Interface | Done; browser-tested end to end (see [`ui/README.md`](ui/README.md)) | `src/labelflow/ui/` |
| 8 | Pilot | Kit ready and dry-run on the real data with made-up accounts; the pilot with real participants not started | `pilot/montgomery/` (recommended), `pilot/jsrt/` (alternative), `pilot/common/` |

**The prototype tag.** The prototype was removed on the Phase 1 branch. Its last commit
on `main` is `e77c9d0`. To tag it, run
`git tag v0-prototype e77c9d0 && git push origin v0-prototype`.

## How the pieces fit

- `labelflow.engine` is plain Python. Every change is an event appended to the log. A pure
  reducer applies each event to a small set of **derived tables** (slots, labels, final
  labels, and so on), in the same transaction. The reducer never calls a label task or a
  clock: draws, consensus answers, ids and timestamps are all inside the events. Replaying
  the log therefore rebuilds the tables exactly, and a property test checks this on both
  stores.
- The engine reaches its tables through four operations (get, insert, update, select by
  equality). `InMemoryStore` and the SQLite `SqlStore` both implement them, and the
  SQLite tables are generated from the same declaration (`labelflow.engine.tables.SCHEMA`).
  Every engine test runs on both stores.
- Each event stores the hash of the one before it (`verify_chain`). SQLite triggers refuse
  any update or deletion of an event, and any event out of chain order.

## Decisions

### Routing and claims (engine)

1. **Decided: a released item is offered back to the same person only last.** In that stage,
   the person who released it gets it again only after every other slot they are eligible
   for. Without this, a release would hand the same slot straight back, since it is first in
   the default order. `explain(item)` says when this is why a slot waits.
2. **Decided: one claim at a time per person per workflow.** Asking for work while holding a claim
   returns that claim. The database enforces this too.
3. **Decided: excluding an item is per workflow,** like its final label. An item that
   already has its final label may still be excluded: the exclusion is a new logged event,
   the item's status becomes `excluded`, and the final label stays in the log as history.
   The items export keeps the earlier final label and adds `final_status`, so only rows
   whose `status` is `final` are usable labels.
4. **Decided: pause and resume.** An admin can pause one stage, or a whole workflow, and
   resume it; each is a logged event with who, when and why. A pause stops claims only: no
   new work is handed out there, but items keep arriving and their slots wait, and claims
   already held can still be submitted or released. A stage's own pause and its workflow's
   pause are independent, so resuming the workflow leaves a paused stage paused.
   `explain(item)` says when a slot waits because of a pause.
5. **Proposed: items not sampled into a first stage** (a stage with no input and `sample` < 1) are
   done at once, with final status `none`.
6. **Proposed: a stage whose slots were all cancelled** gives the item no output, so no later stage
   admits it from there. Once nothing else is open for the item, the final step runs as
   usual over the stages that did label it.
7. **Proposed: `labels_per_item: all` with nobody eligible** at admission opens no slots. The item
   leaves the stage at once with no labels. Starting the workflow and each role change
   warn about this case.
8. **Proposed: two independent draws per stage.** `sample` uses `hash(seed, stage, item)`. The
   `{count, fraction}` form uses `hash(seed, stage, item, "labels_per_item")`, so which items
   are sampled and which get extra labels are independent. A draw is the top 53 bits of
   the SHA-256 digest divided by 2^53, so it never rounds to 1.0. The seed is a whole
   number from 0 to 2^63 − 1. The API picks one at random unless one is given.
9. **Proposed: roles must be defined before use.** Defining a role is its own logged event. Granting
   an undefined role, or starting a workflow that names one, is an error.
10. **Proposed: what a reviewer is shown:** earlier labels of the item from *other* stages of this
   workflow, without labeler names, plus the latest upstream consensus detail. A blind stage
   (`show_prior: false`) shows neither. Item properties, such as a gold key, are never shown.
11. **Proposed: what the interface reports is checked.** The content hash must match the item, and
    the reported earlier labels must be labels of this item in this workflow. A blind stage
    may not report earlier labels.
12. **Proposed: scoring never blocks a submit.** A missing key is logged as `no_key`, and a key that
    is not a valid label as `invalid_key`.
13. **Proposed: ids** for slots and labels are sequential integers, unique within a project.

### Config

14. **Decided: `assign_to` is required.** There is no "everyone" default.
15. **Decided: `sample` must be more than 0.** `sample: 0` is an error. To stop a stage
    handing out work, pause it (item 4).
16. **Proposed: `.agreed` and `.disagreed` need a stage that can give several labels.** They are
    errors on a stage whose `labels_per_item` is 1, because that output would always be
    empty.
17. **Proposed: the coverage rule, exactly.** A stage passes all of its output downstream when one
    of these holds:
    - a later stage takes its whole output with `sample` 1; or
    - it always gives two or more labels (an integer `labels_per_item` ≥ 2), and later
      stages take both `.agreed` and `.disagreed` with `sample` 1.
18. **Proposed: a key written twice in the YAML is an error,** because YAML would otherwise silently
    keep the last one.

### Label task

19. **Decided: every mask class appears in every label.** An empty mask is an RLE with no foreground.
    A missing class is a validation error, so "forgot to draw" and "nothing there" cannot be
    confused.
20. **Proposed: masks of different sizes disagree.** Agreement uses the exact IoU, and reported values
    are rounded to 6 places.
21. **Proposed: the ontology version** is `name@version+fingerprint`, where the fingerprint is a hash
    of the ontology's content. An edited file whose version was not bumped is therefore
    still detectable.
22. **Proposed: both COCO RLE forms are accepted:** compressed strings and uncompressed count lists. Labels
    are stored exactly as submitted.

### Edits to a running workflow

23. **Decided: refused, besides the design notes' list:**
    - reordering existing stages, because order decides the default work order;
    - re-using the name of a removed stage;
    - removing a stage that still has open *or claimed* slots;
    - an edit that changes nothing.

    A removed stage and an added one with the same input and roles get a warning that this
    looks like a rename.
24. **Proposed: the change preview's counts** sort every item into exactly one group per affected
    stage:
    - in the stage now;
    - may still arrive (an upper bound, because upstream sampling and consensus are still
      to come);
    - already past;
    - cannot arrive.

    A sample change also shows how many of the items that may still arrive would be admitted
    under the old and the new value. A `final` change counts items not yet finalized (affected)
    and finalized items (unaffected).
25. **Proposed: making a stage blind mid-way** still accepts labels from people who were shown earlier
    labels when they claimed.

### Storage

26. **Proposed: one SQLite database holds several projects,** each with its own event log and
    fingerprint chain.
27. **Proposed: JSON is stored as text,** so values read back exactly. A JSON column in SQLite would
    turn `1.0` into `1`.
28. **Proposed: Postgres is not supported yet.** `connect()` refuses other databases.

### API

29. **Decided: admins administer every project.** Labelers see the projects and workflows where they
    hold a role. There are no per-project admins in Phase 1.
30. **Proposed: login** issues a random token, kept in the database only as its SHA-256. The token is
    sent as a bearer header or as an HttpOnly, SameSite=lax cookie (for the server-rendered
    interface). Deactivating an account or resetting its password ends its logins.
    Passwords are hashed with the standard library's scrypt.
31. **Proposed: hashing at import, deny by default.** If an item comes without a content
    hash, the server hashes the file only when it is a `file://` path inside
    `LABELFLOW_MEDIA_ROOT`. Without that setting, every item must come with its hash. A
    batch import is all or nothing.
32. **Decided: a held claim survives a role change.** A person whose role is revoked can still submit
    a slot they had already claimed, since they held the role at the moment of the claim,
    even if it was their last role in the project. Their next request for work follows the
    new roles. Labels record the roles held at the claim (`roles_at_claim`) as well as at
    submit.
33. **Proposed: errors** are `{detail, problems}`. Unknown things give 404; conflicts (including a
    stale base version for an edit, and claiming in a paused stage) give 409; "not yours" and "not eligible" give 403;
    invalid input, labels, configs and refused edits give 422.

### Provenance of a review

34. **Decided: a reviewer may start from an earlier label** (Hudson, Oct 11, 2026). In a
    stage that shows earlier labels, "Start from this layer" copies one earlier label's masks
    and classifications into the editor, as one undoable step. The label reports it as
    `shown.started_from`. The engine accepts it only if it is one of the reported
    `prior_label_ids`, so never in a blind stage. It is logged in `label_submitted` (left out
    of the event's JSON when unset, so older events keep their hashes) and stored in
    `labels.started_from` (migration 0005). `explain` says "dr_smith started from label 12".
    The labels export gives `started_from` and `changed_from_start`: the label task's
    consensus check of the label against its start (agreed, rule version, and per-field
    detail such as the IoU per mask class). This is computed at export time from the two
    stored labels and is not stored, so researchers can see whether reviewers edit or
    rubber-stamp. "Clear all" after a start keeps `started_from` (the reviewer did start from
    it); undoing the start removes it.

### The pilot (milestone 8)

35. **Proposed: the pilot runs on the Montgomery County chest X-ray set** (`pilot/montgomery`).
    It replaces JSRT, whose download needs a registration with an expiring password; the
    JSRT kit stays as an alternative.
    - The Montgomery set comes from the NLM, as a single 617 MB zip with no login.
    - It has 138 de-identified 2D radiographs, with a mask of each lung and a TB label each.
    - The terms allow research use and local hosting. They ask that the data not be shared
      outside the research group, and that two papers be cited.

    The Shenzhen set and Kvasir-SEG were the fallbacks. Neither was needed. The Shenzhen
    set has 662 images; the NLM gives it outlines of the abnormalities on the TB images,
    but no lung masks. Kvasir-SEG has no classification.
36. **Decided (Hudson, Oct 11, 2026): `tb_findings: present | absent` means any
    radiographic sign of TB, active or old.** This matches the dataset's label, which
    supplies the gold answer through the file name. 24 of the 58 TB readings describe
    inactive or healed disease, or have no report; their gold answer is `present`. The
    ontology and the participants' section of the kit's README say so.
37. **Decided (Hudson, Oct 11, 2026): lung masks agree at IoU 0.90 for the pilot.** The
    dataset has a single observer, and I found no published inter-observer study of its
    masks. The threshold sits near the mean minus 1.65 sd of the SCR study's human-observer
    lung overlap (0.946 ± 0.018). After the pilot it will be revisited from the agreement of
    our own double reads. A new threshold is a new label-task version (`montgomery_tb@2`).
38. **Decided (by the data): items get opaque names.** Montgomery's file names give the TB
    answer away, through their last digit and through their numbers (every normal image is
    numbered below every TB image), and the labeling page shows an item's name. So items
    are named `mc_` plus 10 hex digits of the source PNG's SHA-256, and are listed in that
    order. The JSRT kit had the same leak (`JPCLN…` with a nodule, `JPCNN…` without). Its
    items are now named `jsrt_` plus the hash of the `.IMG` file.
39. **Decided (Hudson: "make up the accounts for now"): pilot accounts are made up and no
    password is in the repository.**
    - `accounts.example.csv` names fictional people.
    - `setup_pilot.py` generates every password and appends them to `<kit>/out/credentials.csv`.
      That file is git-ignored and has mode 0600. The script prints a warning to hand the
      passwords out privately.
    - The role `admin` in the CSV makes a server administrator.
40. **Decided (Hudson, Oct 11, 2026): the gold set has 12 images, stratified on the TB answer
    (5 and 7), and each patient's images stay on one side of the split.**
    - The main set has the other 126 images.
    - The split joins the images of one patient through `same_patient_as`, following the
      links both ways and through chains. It then draws whole patients, seeded and
      stratified; a patient that would overshoot a stratum's share is skipped.
    - On the real data the counts are unchanged: 5 TB and 7 normal in the gold set, 53 and
      73 in the main set. Both multi-image patients (three images and two) are in the main
      set.

## Hardening after review (Oct 11, 2026)

A read-only review of the first merge found these, all now fixed:

- **Mask sizes are bounded** (16384 pixels per side, 64 megapixels; an ontology may set a
  lower `max_mask_pixels`). Sizes are checked before any mask is decoded, and agreement is
  computed from the run lengths, so a tiny label claiming a huge mask cannot exhaust memory.
- **Request sizes are bounded:** request bodies, labels, item properties and import batches
  (see the `LABELFLOW_MAX_*` settings in the README).
- **A config rule:** two stages that can hold the same item at the same time may not share
  a role when either uses `labels_per_item: all`, since nobody labels an item twice and the
  item would never finish. Staffing warnings count people across such stages.
- **Reserved slots that can no longer be filled are cancelled** (logged) when their person
  labels the item in another stage.
- **An ontology edited under a running workflow** is refused with a plain message instead of
  failing; a failing label task never leaves a half-done command.
- **What the interface reports as shown** must match what the stage shows.
- **Deactivating an account revokes its roles** in every project (logged).
- **People without a role in a project** cannot see it exist, and get no name suggestions.

## Known follow-ups

- Login rate limiting and account reactivation.
- The interface's `shown` reports every earlier-label layer the work packet offered, not
  which layers the reviewer actually toggled on. Decide whether toggles should be logged.
- Mermaid diagrams load from a CDN in the admin pages (vendoring it is about 3 MB).
- The home page's "work waiting" count reads engine internals; it should get an engine
  read method.
- Locating config problems by YAML line number. Today they are located by stage and key.

## The design notes' open questions, answered in review (Oct 11, 2026)

- **Proposed items in the design notes:** the output names `.agreed` and `.disagreed`, the
  `final` defaults and `final: none`, the default work order, and the details of what can be
  edited in place are all confirmed.
- **Names:** the package is `labelflow`; `show_prior` stays.
- **Mask tools:** both brush and polygon, editing the same one-mask-per-class label. The
  interface spike chooses a drawing library that supports both.
- **Agreed label for masks:** a copy of the earliest submitted label is acceptable for the
  anchor project. A merge can come later, as a new label task version.
- **Choosing between workflows:** the labeler picks. The interface lists their workflows with
  how much work waits in each.
