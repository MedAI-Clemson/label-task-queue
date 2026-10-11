# The interface (milestone 7)

The server-rendered pages live in `src/labelflow/ui/` and are mounted under `/ui` of the
API app (`/` redirects there). They share the API's accounts, login cookie and engines.

## Pages

| Who | Page | What it does |
| --- | --- | --- |
| everyone | `/ui/login`, `/ui/logout` | Form login setting the same `HttpOnly`, `SameSite=lax` cookie the API uses |
| labeler | `/ui/` | The projects you hold a role in, each workflow with the open slots you could claim now (you pick the workflow) |
| labeler | `/ui/projects/{p}/workflows/{w}` | The workflow's stages and pauses, and "Get work" (claims the next slot) |
| labeler | `.../slots/{slot}` | The labeling page: image, brush and polygon mask tools per class, erase, undo/redo, zoom/pan, classifications, earlier labels as toggleable layers with agree/disagree flags (none in a blind stage), submit, release with a reason |
| labeler | `.../slots/{slot}/image` | Serves a `file://` image or a path inside `LABELFLOW_MEDIA_ROOT`, only to the holder of the slot |
| admin | `/ui/admin/accounts` | Create, deactivate, reset password |
| admin | `/ui/admin/projects`, `/ui/admin/projects/{p}` | Create projects; define roles, grant and revoke (with staffing warnings); create datasets |
| admin | `.../datasets/{d}` | Items, and import from pasted JSON or CSV (hashed by the server when it can read the file) |
| admin | `.../workflows/new` | Upload a YAML config with a live check (HTMX): errors, warnings, resolved YAML, Mermaid diagram; start with an optional seed |
| admin | `.../workflows/{w}` | Progress, pause/resume (workflow and stage), cancel open slots, exclude an item, diagram and resolved config, versions with diffs, exports (CSV/Parquet) and reports, preview and activate an edit |
| admin | `.../workflows/{w}/explain?item=` | `explain(item)` as a table and as text |

Submitting a label is a JSON post from the page to the API's own
`POST /projects/{p}/workflows/{w}/slots/{slot}/submit` with `label`, `shown` and
`duration_ms`. `shown` is the work packet's `shown` exactly as the server gave it: the page
offers every earlier label of the packet as a layer, and a blind stage has none.

Forms carry a token (an HMAC of the login cookie) checked on every post; the cookie is
`SameSite=lax`, so a cross-site post carries neither.

## Running it

    ./run.sh                          # alembic upgrade head, then uvicorn on :8000
    # first time: python -m labelflow.api.admin create-admin <username>
    # images: set LABELFLOW_MEDIA_ROOT to the folder the items' paths are relative to

Open `http://localhost:8000/ui/`. The Mermaid diagram is drawn by `mermaid@11.4.1` from
jsdelivr; without outbound access the diagram's source is shown as text instead.

## Tests

    pytest tests/ui                   # every page and form through the TestClient
    pip install playwright==1.48.0    # matches chromium-1140 in ~/.cache/ms-playwright
    LABELFLOW_SCREENSHOT_DIR=docs/ui pytest tests/ui/test_browser.py -q   # the browser run

`test_browser.py` runs the review example end to end in headless Chromium (an admin sets
up accounts, roles, dataset, items and the workflow through the pages; an annotator draws
with the brush and the polygon, classifies and submits; the expert reviews with layers),
then checks the event log: each label's `shown` equals what the page displayed, and the
masks decode to exactly the drawn pixels. It is skipped when Playwright is not installed.
The screenshots here come from that run.
