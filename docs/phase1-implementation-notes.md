# label-task-queue: Phase 1 Implementation Notes

Oct 10, 2026 · companion to [`phase1-design-notes.md`](phase1-design-notes.md)

This note records where the implementation stands against the design notes' plan, and
every decision the implementation had to make that the design notes leave open. Each
decision is **proposed**: it is what the code does now, and it needs a yes or no. Changing
one is usually a small, local change.

## Status by milestone

| # | Milestone | Status | Where |
| --- | --- | --- | --- |
| 0 | Scaffold | Done, except the `v0-prototype` tag (see below) | `pyproject.toml`, `.github/workflows/ci.yml` |
| 0b | Interface spike | Not started: needs a person to try the drawing libraries | |
| 1 | Config | Done | `src/labelflow/engine/config/`, `schema/`, `examples/workflows/` |
| 2 | Engine core | Done | `src/labelflow/engine/` |
| 3 | Built-in label task | Done | `src/labelflow/labeltask/`, `examples/ontologies/` |
| 4 | Storage | Done (SQLite only) | `src/labelflow/store/`, `alembic.ini` |
| 5 | Versioning and edits | Done | `src/labelflow/engine/edits.py` |
| 6 | API | Done (Docker image not built in CI) | `src/labelflow/api/` |
| 7 | Interface | Not started: waits on 0b | |
| 8 | Pilot | Not started | |

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

## Decisions to confirm

### Routing and claims (engine)

1. **A released item is not offered back to the same person in that stage.** Without this,
   a release would hand the same slot straight back, since it is first in the default order.
   An admin can still cancel the slot. `explain(item)` says when this is why a slot waits.
2. **One claim at a time per person per workflow.** Asking for work while holding a claim
   returns that claim. The database enforces this too.
3. **Excluding an item is per workflow,** like its final label. Excluding an item that
   already has its final label is refused.
4. **Items not sampled into a first stage** (a stage with no input and `sample` < 1) are
   done at once, with final status `none`.
5. **A stage whose slots were all cancelled** gives the item no output, so no later stage
   admits it from there. Once nothing else is open for the item, the final step runs as
   usual over the stages that did label it.
6. **`labels_per_item: all` with nobody eligible** at admission opens no slots. The item
   leaves the stage at once with no labels. Starting the workflow and each role change
   warn about this case.
7. **Two independent draws per stage.** `sample` uses `hash(seed, stage, item)`. The
   `{count, fraction}` form uses `hash(seed, stage, item, "labels_per_item")`, so which items
   are sampled and which get extra labels are independent. A draw is the top 53 bits of
   the SHA-256 digest divided by 2^53, so it never rounds to 1.0. The seed is a whole
   number from 0 to 2^63 − 1. The API picks one at random unless one is given.
8. **Roles must be defined before use.** Defining a role is its own logged event. Granting
   an undefined role, or starting a workflow that names one, is an error.
9. **What a reviewer is shown:** earlier labels of the item from *other* stages of this
   workflow, without labeler names, plus the latest upstream consensus detail. A blind stage
   (`show_prior: false`) shows neither. Item properties, such as a gold key, are never shown.
10. **What the interface reports is checked.** The content hash must match the item, and
    the reported earlier labels must be labels of this item in this workflow. A blind stage
    may not report earlier labels.
11. **Scoring never blocks a submit.** A missing key is logged as `no_key`, and a key that
    is not a valid label as `invalid_key`.
12. **Ids** for slots and labels are sequential integers, unique within a project.

### Config

13. **`assign_to` is required.** There is no "everyone" default.
14. **`sample` must be more than 0.** `sample: 0` is an error, not a way to switch a stage off.
15. **`.agreed` and `.disagreed` need a stage that can give several labels.** They are
    errors on a stage whose `labels_per_item` is 1, because that output would always be
    empty.
16. **The coverage rule, exactly.** A stage passes all of its output downstream when one
    of these holds:
    - a later stage takes its whole output with `sample` 1; or
    - it always gives two or more labels (an integer `labels_per_item` ≥ 2), and later
      stages take both `.agreed` and `.disagreed` with `sample` 1.
17. **A key written twice in the YAML is an error,** because YAML would otherwise silently
    keep the last one.

### Label task

18. **Every mask class appears in every label.** An empty mask is an RLE with no foreground.
    A missing class is a validation error, so "forgot to draw" and "nothing there" cannot be
    confused.
19. **Masks of different sizes disagree.** Agreement uses the exact IoU, and reported values
    are rounded to 6 places.
20. **The ontology version** is `name@version+fingerprint`, where the fingerprint is a hash
    of the ontology's content. An edited file whose version was not bumped is therefore
    still detectable.
21. Both COCO RLE forms are accepted: compressed strings and uncompressed count lists. Labels
    are stored exactly as submitted.

### Edits to a running workflow

22. **Refused, besides the design notes' list:**
    - reordering existing stages, because order decides the default work order;
    - re-using the name of a removed stage;
    - removing a stage that still has open *or claimed* slots;
    - an edit that changes nothing.

    A removed stage and an added one with the same input and roles get a warning that this
    looks like a rename.
23. **The change preview's counts** sort every item into exactly one group per affected
    stage:
    - in the stage now;
    - may still arrive (an upper bound, because upstream sampling and consensus are still
      to come);
    - already past;
    - cannot arrive.

    A sample change also shows how many of the items that may still arrive would be admitted
    under the old and the new value. A `final` change counts items not yet finalized (affected)
    and finalized items (unaffected).
24. **Making a stage blind mid-way** still accepts labels from people who were shown earlier
    labels when they claimed.

### Storage

25. **One SQLite database holds several projects,** each with its own event log and
    fingerprint chain.
26. **JSON is stored as text,** so values read back exactly. A JSON column in SQLite would
    turn `1.0` into `1`.
27. **Postgres is not supported yet.** `connect()` refuses other databases.

### API

28. **Admins administer every project.** Labelers see the projects and workflows where they
    hold a role. There are no per-project admins in Phase 1.
29. **Login** issues a random token, kept in the database only as its SHA-256. The token is
    sent as a bearer header or as an HttpOnly, SameSite=lax cookie (for the server-rendered
    interface). Deactivating an account or resetting its password ends its logins.
    Passwords are hashed with the standard library's scrypt.
30. **Hashing at import:** if an item comes without a content hash, the server hashes the
    file. It does this only for a `file://` path inside `LABELFLOW_MEDIA_ROOT`, or for any
    `file://` path when that setting is unset. Otherwise it asks for the hash. A batch
    import is all or nothing.
31. **A held claim survives a role change.** A person whose role is revoked can still submit
    a slot they had already claimed, since they held the role at the moment of the claim.
    Their next request for work follows the new roles.
32. **Errors** are `{detail, problems}`. Unknown things give 404; conflicts (including a
    stale base version for an edit) give 409; "not yours" and "not eligible" give 403;
    invalid input, labels, configs and refused edits give 422.

## Known follow-ups

- The API reads some derived tables and the event log directly. The engine should offer
  read methods for these, and a read-only transaction: today reads take the write lock,
  which is fine at Phase 1 scale but serializes everything.
- `Engine.import_items` (an atomic batch) would replace the API's loop.
- Login rate limiting, account reactivation, and a form-encoded login for the interface.
- Locating config problems by YAML line number. Today they are located by stage and key.

## Open questions from the design notes, as the code answers them for now

- **Choosing between workflows:** a labeler asks one workflow for work at a time. The
  interface will let them pick.
- **Agreed label for masks:** a copy of the earliest label, as decided. There is no merge.
- **Mask tool (brush or polygon):** open, pending the interface spike.
- **Names:** the package is called `labelflow` as a placeholder; `show_prior` is used as proposed.
