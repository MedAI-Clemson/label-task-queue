# Interface spike (milestone 0b): drawing library for brush and polygon masks

**Decision:** build the phase 1 mask tools on **plain Canvas 2D with a small ES module of our
own (`maskcore.js`, about 400 lines with comments, no dependencies)**, and do not take a drawing library.
Konva is the fallback if milestone 7 turns out to need rich vector editing (draggable vertices,
many overlay shapes); the prototype shows it drops in as a view layer without touching the mask
code.

Everything here is in `spikes/mask-tools/`: two working prototype pages (`index.html` on Canvas
2D, `konva.html` on Konva 10.7.1), the shared raster core, a fixture work packet, a browser
test harness, and screenshots. The decision note is this file.

## Why the choice is not really about a library

Every candidate draws **vectors**: paths, polygons, free-hand lines, rendered anti-aliased at
the screen's resolution. What the label task stores is a **raster**: one binary mask per class
on the image's own pixel grid, exported as COCO RLE and compared by IoU. No candidate turns a
brush stroke or polygon into exactly-defined mask pixels; each would need the same owned code to
rasterize to the image grid, and that code is where correctness lives (the harness checks it
pixel for pixel). Once the raster core exists, what a library still offers is the viewport
(zoom, pan), layers, overlays (polygon preview, brush cursor) and pointer events, which are each
a few dozen lines of Canvas 2D. So the comparison is: how much does each library help with that
remainder, and what does it cost?

## Candidates

Versions and dates are from the npm registry and GitHub on 2026-10-11.

| | Plain Canvas 2D + `maskcore.js` | Konva 10.7.1 | Fabric.js 7.4.0 | Annotorious 3.9.4 | OpenLayers 10.11.0 |
| --- | --- | --- | --- | --- | --- |
| License | ours (project license) | MIT | MIT | BSD-3-Clause | BSD-2-Clause |
| Load | one `<script type=module>`; 13 KB unminified | `konva.min.js` 194 KB, script tag or ESM | `index.min.js` 299 KB, ESM/UMD | `annotorious.js` 117 KB + CSS; depends on `@annotorious/core`, `rbush`, `simplify-js`, `uuid` | 12.8 MB unpacked, ESM, meant for a bundler |
| Maintenance | ours | 14.9k stars, 2 open issues, published 2026-10-05 | 31.5k stars, 471 open issues, published 2026-05-18 | 0.9k stars, 12 open issues, published 2026-10-06 | 12.6k stars, 858 open issues, published 2026-10-09 |
| Brush + polygon on a shared raster per class | yes, by construction (`paintStroke`, `paintPolygon` on one `Uint8Array` per class) | not built in; needs the same core; `Konva.Image` shows the mask canvas | not built in; `PencilBrush`/`CircleBrush` make path objects, the eraser moved out of core (`@erase2d/fabric` 1.2.1); needs the same core | no: vector annotations only (polygon, rectangle; W3C annotation model), no brush, no raster | no: vector features; no brush |
| Rasterize exactly to the image pixel grid | yes, our rules (below), verified in the harness | only via the same core; Konva's own canvases are sized by devicePixelRatio | same | n/a | n/a |
| Zoom/pan on large images | wheel zoom + drag, about 25 lines (`setTransform`) | built in (`stage.draggable`, `stage.scale`) | built in (`viewportTransform`) | via OpenSeadragon for deep zoom (extra 5.8 MB package) | built in, tiles; its strength |
| Undo | bbox-cropped snapshots in the core (library-independent) | same | same (Fabric has no undo) | has undo for annotations | n/a |
| Layers for prior labels | one offscreen canvas per layer, `drawImage` in order | `Konva.Image` per layer, `visible()` | `FabricImage` per layer | one annotation set; styling per annotation | vector layers |
| Fit with Jinja2 + HTMX | best: an ES module mounted on one `<div>`, no build step | good: script tag, no build | fair: ESM only in v6+, larger | fair: expects to own the image element | poor: needs a bundler |
| Pitfalls found | none beyond writing the rules | synthetic `dblclick` fires for any two clicks within 400 ms anywhere on the stage (broke polygon closing; fixed by using the native event); every layer is a full-size canvas; `pixelRatio` must be kept in mind for anything that reads pixels back | object model is far from what we need; large; eraser as separate package | wrong shape for raster masks | far too heavy for the job |

Also looked at and set aside: **OpenSeadragon 6.1.1** (BSD-3, deep-zoom tiling; only
needed if images exceed what one canvas holds, see open issues), **Paper.js 0.12.18** (MIT, last
published 2024, vector only), **Pixi.js 8.22** (MIT, WebGL, 76 MB unpacked; overkill), and
**Cornerstone3D 5.11.7** (MIT; has real brush/segmentation tools, but it is a DICOM viewer
stack with its own rendering, image loaders and segmentation model; the right thing for the
3D-connector phase, not for a two-tool 2D page).

## Recommendation and reasons

1. **Correctness is in the raster core either way.** With or without a library, the brush,
   polygon, erase, undo and RLE export are owned code. The harness proves that code against the
   project's `labelflow.labeltask.rle` and `OntologyLabelTask.validate`.
2. **The remainder is small.** The Canvas 2D page's view/overlay code (`app.js`) is about 500
   lines including the toolbar and keyboard handling; the Konva version is the same length,
   because Konva replaces one kind of boilerplate with another (node attributes, `batchDraw`,
   `strokeScaleEnabled`, per-layer smoothing flags) and adds its own event semantics.
3. **No dependency to track and no CDN.** The system is self-hosted and must not assume
   outbound access; a vendored 194 KB library is manageable but a 13 KB module of our own is
   simpler to audit, and HTMX page swaps only need to re-run one `mount()`.
4. **Performance is the same**: both pages share the core, and the measured costs (below) are in
   the core and the compositing, not the library.
5. **Konva stays the fallback.** `konva.html` shows the swap is mechanical: the same
   `panel.js` and `maskcore.js`, a different view file. If milestone 7 wants draggable polygon
   vertices, selection handles or many overlay shapes, switch the view file to Konva (MIT,
   well-maintained, loads from a script tag).

## Rasterization rules (what a label means, pixel for pixel)

These are implemented once in `spikes/mask-tools/maskcore.js` and re-implemented in numpy in
`check/check_export.py`; the harness requires the two to agree exactly.

- **Grid.** Mask pixel `(x, y)` covers the image square `[x, x+1) x [y, y+1)`; its **center** is
  `(x + 0.5, y + 0.5)`. The mask grid is the natural size of the image file the packet's `uri`
  points at. Pointer positions are continuous image coordinates (client pixel minus the view
  translation, divided by the scale); nothing is snapped except the brush center.
- **Brush.** The brush is centered on the center of the pixel under the pointer. A dot of
  diameter `d` sets every pixel whose center is within `d / 2` (inclusive) of that center: a
  1 px brush paints exactly the pixel under the cursor, and `d = 11` paints the 97 pixels with
  integer offsets `dx^2 + dy^2 <= 30.25`. A stroke is the union of capsules (pixels whose center
  is within `d / 2` of the segment) between consecutive pointer samples, so fast movement leaves
  no gaps. Erase is the same shape clearing pixels.
- **Polygon.** Vertices are the clicked continuous positions. A pixel is filled when its center
  is inside the polygon by the **even-odd rule**, with the half-open scanline convention: an edge
  counts for the scanline at `y + 0.5` when exactly one endpoint has `y <= y + 0.5`, and a span
  `[xa, xb)` covers the centers with `xa <= x + 0.5 < xb`. Consequences: an axis-aligned
  rectangle with integer corners fills exactly `(x1 - x0) * (y1 - y0)` pixels (the 80 x 50 test
  gives 4000); two polygons sharing an edge neither overlap nor leave a gap; a self-crossing
  polygon leaves its doubly-covered parts empty. Polygon erase uses the same fill clearing
  pixels.
- **Boundary arithmetic** is exact where inputs are half-integers (squared distances are compared
  as `cross^2 <= r^2 * len^2`), which is why the numpy replay can match bit for bit.
- **Export.** COCO RLE, column-major, first run background (possibly 0), compressed string by
  default (the `rleToString` scheme) or uncompressed list; both are produced and both decode
  identically with `labelflow.labeltask.rle.decode`. Re-encoding the decoded mask with
  `rle.encode` gives a byte-identical string. Every mask class is always present; an untouched
  class exports as an empty mask.

Not snapping polygon vertices to the grid is deliberate: with the center rule the result is
still crisp, and snapping would make the preview disagree with the fill at high zoom.

## What the prototype does

Both pages load a fixture `Work` response (`fixtures/work.json`: slot, `WorkPacket`, `shown`)
and the ontology (`fixtures/ontology.json`, generated from `examples/ontologies/chest_xray.yaml`),
then offer:

- image view with wheel zoom about the cursor, pan (tool, shift-drag, or middle/right drag), fit;
- mask class picker from the ontology with live pixel areas;
- brush (paint/erase, 1 to 100 px, `[` `]`) with a cursor showing the exact footprint;
- polygon (click vertices; close by clicking the first vertex, double-click or Enter; Backspace
  removes the last vertex; Esc cancels), also in erase mode (polygon-erase);
- undo/redo across tools and classes (Ctrl+Z / Ctrl+Shift+Z), with undo entries cropped to the
  edited box so memory stays small on large images;
- classifications as selects with the ontology's allowed values, required ones marked;
- reviewer mode: each prior label's masks as toggleable layers, and the consensus check's
  per-field agree/disagree flags with IoU and threshold;
- export of the submit body `{label, shown, duration_ms}`; `shown` is the packet's `shown`
  with `prior_label_ids` set to the earlier labels the page offered, `duration_ms` is measured
  from page open;
- a pixel grid at 8x zoom and above (Canvas 2D page only), useful for checking edges.

Screenshots: `spikes/mask-tools/screenshot-index.png` (after the harness run: dot, stroke with
an erased hole, rectangle with a polygon-erased hole, triangle, one prior layer hidden) and
`screenshot-index-zoom.png` (12x on the erased hole, pixel grid on).

## How it was verified

`check/run_check.mjs` drives the page in headless Chromium through real pointer and keyboard
events (Playwright 1.48.2, matching the cached `chromium-1140`), at a view of scale 1 with an
integer translation so that integer client pixels land on integer image coordinates. It paints
a brush dot (d = 11), a brush stroke, an 80 x 50 rectangle polygon closed by clicking its first
vertex, a triangle closed with Enter, a brush erase, a 20 x 20 polygon erase, an undo/redo/undo
sequence through the keyboard, sets classifications through the DOM, hides a prior layer, clicks
Export, and writes the submit body (both RLE forms), the ordered op log and timings to
`check/out/`. `check/check_export.py` then, with the project venv:

- validates the label with `OntologyLabelTask.validate` on `chest_xray.yaml`
  (`image_ontology@1+b760ade3a9e3`), checks the `shown` fields and `duration_ms`;
- decodes both RLE forms with `labelflow.labeltask.rle.decode` and requires them equal, and
  `rle.encode` of the decoded mask to be byte-identical to the page's compressed string;
- replays the op log (strokes, polygons, undo, redo) with the numpy versions of the rules and
  requires **0 differing pixels** in every class;
- checks the known shapes: the dot is its 97 centers, the rectangle is 4000 px, the polygon
  erase removed 400 px, the effusion mask is exactly the triangle (3560 px).

All checks pass on both pages, at devicePixelRatio 1 and 2.

## Performance on a 2048 x 2048 image (headless Chromium, cluster login node, DPR 1)

Numbers from `check/out/perf-index.json` over three runs (the Konva page is within noise, as
expected since the core is shared).

| Operation | Time |
| --- | --- |
| Page load with image and prior-label decode | 250 to 380 ms (decode itself 1 to 2 ms) |
| Brush stroke of 400 points, d = 40, across the image (core only) | 90 to 130 ms |
| Real pointer stroke, 60 move events at fit scale, d = 30: page work in handlers | 9.4 ms total, 0.16 ms per move |
| Polygon fill of about half the image (2.1 M px) | 90 to 130 ms |
| Polygon erase, 100-vertex circle of radius 500 | 47 to 62 ms |
| Undo / redo of that (bbox crop + paste) | 39 to 56 ms / 27 ms |
| Full redraw at fit scale / at 8x zoom | 8 to 10 ms / 3.5 ms |
| Export, compressed / uncompressed (1.5 M px lung mask) | 51 to 71 ms, 13.4 KB / 45 to 49 ms, 28 KB |
| JS heap after all of the above | 36 to 78 MB |

Memory per 2048 x 2048 class: 4 MB for the mask bytes plus 16 MB for its tinted RGBA canvas;
the same 16 MB per prior-label layer. Two classes and one prior label use about 80 MB of canvas
memory, which is fine; 4096 x 4096 would be four times that (see open issues). The real-stroke
wall time (about 1.9 s for 60 steps) is Playwright's per-step round trip, not the page.

## Open issues for milestone 7

1. **Mounting under HTMX.** The page swaps need a `mount(container, workJson, ontologyJson)`
   entry point and an unmount; submit is a JSON `fetch` to `POST .../slots/{slot}/submit`
   (not a form post; the body can be hundreds of KB for busy masks), with the 422 problem list
   rendered next to the tools.
2. **What `shown` should report after a layer is toggled off.** The prototype reports every
   prior label the packet offered. If "shown" must mean "actually visible at some point", record
   layer toggles client-side and report the set that was ever visible; the engine accepts any
   subset of the item's labels.
3. **Mask grid versus image file.** The mask size is the natural size of the file at `uri`. If a
   deployment serves a downsampled or re-encoded image for display, the label grid silently
   changes; the server should state the expected size in the packet (or the interface should
   send it back) so the label task can refuse a mismatch.
4. **Very large images.** Above about 4096 x 4096 the per-class RGBA canvases get heavy; options
   are a single composited RGBA canvas for all classes, 1-bit storage, or tiling with
   OpenSeadragon. Chest radiographs (about 2000 to 3000 px) do not need any of this.
5. **16-bit and DICOM input.** The page draws whatever `<img>` can decode (8-bit PNG/JPEG). No
   window/level. A DICOM source needs a decoder or a server-side conversion at import.
6. **Polygon ergonomics.** No vertex dragging before closing, no snapping, no hole drawing other
   than polygon-erase. Each is 30 to 60 lines on Canvas 2D; vertex dragging is where Konva would
   earn its keep if asked for.
7. **Class exclusivity.** Masks of different classes may overlap (one raster per class). If the
   ontology later wants exclusive classes, painting one class should erase the others; the core
   supports it (apply the same shape with value 0 to the other classes in one undo entry).
8. **Touch and pen.** Pointer events are used, so pens work; pressure is ignored and there is no
   two-finger pan/zoom yet.
9. **Undo depth.** 50 entries, bbox-cropped; a very long stroke across the whole image stores a
   full-size patch (4 MB at 2048 x 2048). Acceptable; could cap by bytes instead.
10. **Pixel grid on the Konva page** is not implemented (Canvas 2D page only).

## How to run

Serve the folder (ES modules do not load from `file://`) and open the page:

    python -m http.server -d spikes/mask-tools 8008
    # http://localhost:8008/index.html   (Canvas 2D)
    # http://localhost:8008/konva.html   (Konva comparison)
    # add ?work=fixtures/work_2048.json after generating the big fixture (below)

Regenerate fixtures (the 512 image and packet are committed; the 2048 ones are not):

    <venv>/bin/python spikes/mask-tools/fixtures/make_fixtures.py [--big]

Run the checks (nothing is added to the project; Playwright lives in a scratch folder whose
Chromium build matches `~/.cache/ms-playwright`, 1.48.x for `chromium-1140`):

    mkdir -p /tmp/spike-pw && (cd /tmp/spike-pw && npm init -y >/dev/null && npm i playwright@1.48.2)
    PYTHON=<venv>/bin/python NODE_PATH=/tmp/spike-pw/node_modules spikes/mask-tools/check.sh [--big] [--page konva.html]
    DPR=2 PYTHON=... NODE_PATH=... spikes/mask-tools/check.sh        # at devicePixelRatio 2

`--big` generates the 2048 fixture if missing and writes `check/out/perf-<page>.json`. Outputs
go to `spikes/mask-tools/check/out/` (gitignored); the two screenshots of `index.html` are
written next to the page. The harness is deliberately not part of the pytest suite.
