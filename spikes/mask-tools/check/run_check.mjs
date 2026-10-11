// run_check.mjs — drives the prototype page in headless Chromium with real mouse events,
// exports the submit body, and writes everything check_export.py needs to check/out/.
//
// Usage (see check.sh for the full flow):
//   NODE_PATH=<scratch node_modules> [DPR=2] node spikes/mask-tools/check/run_check.mjs [--page index.html] [--big]
//
// Playwright is NOT a project dependency: NODE_PATH must point at a scratch install whose
// Chromium build matches ~/.cache/ms-playwright (playwright 1.48.x for chromium-1140).

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(process.env.NODE_PATH ? path.join(process.env.NODE_PATH, "x.js") : import.meta.url);
const { chromium } = require("playwright");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const OUT = path.join(HERE, "out");
const args = process.argv.slice(2);
const PAGE = args.includes("--page") ? args[args.indexOf("--page") + 1] : "index.html";
const BIG = args.includes("--big");
const DPR = Number(process.env.DPR || 1); // devicePixelRatio of the test browser
const tag = PAGE.replace(/\.html$/, "");
// Screenshots of the primary page are kept in the repo; those of other pages go to check/out.
const SHOTS = PAGE === "index.html" && DPR === 1 ? ROOT : OUT;
fs.mkdirSync(OUT, { recursive: true });

// --- A tiny static server: ES modules do not load from file:// ----------------------------
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png" };
const server = http.createServer((req, res) => {
  const file = path.join(ROOT, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}/`;

const browser = await chromium.launch();
const failures = [];
function expect(condition, message) {
  if (!condition) failures.push(message);
  console.log(`${condition ? "ok  " : "FAIL"} ${message}`);
}

// --- Functional run on the 512 x 512 fixture, driven by real pointer events -----------------
{
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, deviceScaleFactor: DPR });
  page.on("pageerror", (e) => failures.push(`page error: ${e.message}`));
  await page.goto(`${base}${PAGE}`);
  await page.evaluate(() => window.spike.ready);
  const api = (fn, ...a) => page.evaluate(([f, args]) => window.spike[f](...args), [fn, a]);
  const client = async (x, y) => api("toClient", x, y);
  const click = async (x, y) => {
    const c = await client(x, y);
    await page.mouse.click(c.x, c.y);
  };

  // Scale 1 with integer translation: integer client pixels land on integer image coords.
  await api("setView", 1, 40, 40);
  const t = await client(0, 0);
  expect(Number.isInteger(t.x) && Number.isInteger(t.y), `view maps image origin to integer client px (${t.x}, ${t.y})`);

  // 1. Brush dot, diameter 11, on 'lung'.
  await api("selectClass", "lung");
  await api("setTool", "brush");
  await api("setBrush", 11);
  await click(100, 100);
  const dotArea = await api("area", "lung");
  expect(dotArea > 0, `brush dot painted ${dotArea} px`);

  // 2. Brush stroke, diameter 20, left to right with several pointer moves.
  await api("setBrush", 20);
  let c = await client(200, 150);
  await page.mouse.move(c.x, c.y);
  await page.mouse.down();
  c = await client(300, 150);
  await page.mouse.move(c.x, c.y, { steps: 8 });
  await page.mouse.up();

  // 3. Rectangle polygon with integer corners, closed by clicking the first vertex.
  await api("setTool", "polygon");
  const before = await api("area", "lung");
  for (const [x, y] of [[320, 320], [400, 320], [400, 370], [320, 370], [320, 320]]) await click(x, y);
  const rectArea = (await api("area", "lung")) - before;
  expect(rectArea === 80 * 50, `rectangle polygon 80 x 50 fills exactly ${rectArea} px (expected 4000)`);

  // 4. Triangle on 'effusion', closed with Enter.
  await api("selectClass", "effusion");
  for (const [x, y] of [[50, 400], [150, 420], [90, 480]]) await click(x, y);
  await page.keyboard.press("Enter");
  expect((await api("area", "effusion")) > 0, `triangle painted ${await api("area", "effusion")} px on effusion`);

  // 5. Erase with the brush (a dot out of the stroke) and with a polygon (out of the rectangle).
  await api("selectClass", "lung");
  await api("setErase", true);
  await api("setTool", "brush");
  await api("setBrush", 6);
  await click(250, 150);
  await api("setTool", "polygon");
  const beforeErase = await api("area", "lung");
  for (const [x, y] of [[340, 330], [360, 330], [360, 350], [340, 350]]) await click(x, y);
  await page.keyboard.press("Enter");
  expect(beforeErase - (await api("area", "lung")) === 400, `polygon erase removed exactly 400 px`);
  await api("setErase", false);

  // 6. Undo / redo through the keyboard.
  await api("setTool", "brush");
  const beforeDot = await api("area", "lung");
  await click(480, 480);
  await page.keyboard.press("Control+z");
  expect((await api("area", "lung")) === beforeDot, "undo restores the area");
  await page.keyboard.press("Control+Shift+z");
  expect((await api("area", "lung")) > beforeDot, "redo re-applies the dot");
  await page.keyboard.press("Control+z");

  // 7. Classifications and the prior-label layer toggle, through the DOM.
  await page.selectOption("#cls-view", "PA");
  await api("setClassification", "quality", "poor");
  await page.uncheck('#layers input[data-layer="1"]');

  await page.click("#export");
  const text = await page.inputValue("#output");
  expect(text.includes('"duration_ms"'), "export textarea holds a submit body");
  const submit = await api("export", true);
  const submitUncompressed = await api("export", false);
  const opLog = await api("opLog");
  const timings = await api("timings");
  const outPath = path.join(OUT, `export-${tag}.json`);
  fs.writeFileSync(outPath, JSON.stringify({ page: PAGE, submit, submitUncompressed, opLog, timings }, null, 1));
  console.log(`wrote ${outPath} (${opLog.length} ops)`);

  await page.screenshot({ path: path.join(SHOTS, `screenshot-${tag}.png`) });
  // A zoomed view on the rectangle's erased corner, with the pixel grid.
  await api("setView", 12, -340 * 12 + 300, -330 * 12 + 300);
  await page.screenshot({ path: path.join(SHOTS, `screenshot-${tag}-zoom.png`) });
  await page.close();
}

// --- Performance on the 2048 x 2048 fixture (generated by make_fixtures.py --big) -------------
if (BIG) {
  if (!fs.existsSync(path.join(ROOT, "fixtures", "work_2048.json"))) {
    failures.push("fixtures/work_2048.json is missing; run make_fixtures.py --big");
  } else {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
    page.on("pageerror", (e) => failures.push(`page error (big): ${e.message}`));
    const t0 = Date.now();
    await page.goto(`${base}${PAGE}?work=fixtures/work_2048.json`);
    await page.evaluate(() => window.spike.ready);
    const loadMs = Date.now() - t0;
    const perf = await page.evaluate(() => {
      const s = window.spike;
      const out = { priorDecodeMs: s.timings().priorDecodeMs };
      const time = (f) => {
        const a = performance.now();
        f();
        return performance.now() - a;
      };
      s.selectClass("lung");
      // A zigzag brush stroke of 400 points, diameter 40, across the whole image.
      const zig = [];
      for (let i = 0; i < 400; i++) zig.push([100 + i * 4.5, 1024 + Math.sin(i / 8) * 800]);
      out.stroke400ptsD40Ms = time(() => s.applyStroke(zig, 40, false));
      out.strokeTimingInternalMs = s.timings().lastStrokeMs;
      // A large diamond polygon, about half the image area.
      const diamond = [[1024, 30], [2018, 1024], [1024, 2018], [30, 1024]];
      out.polygonHalfImageMs = time(() => s.applyPolygon(diamond, false));
      out.polygonErase100ptsMs = time(() => {
        const v = [];
        for (let i = 0; i < 100; i++) v.push([1024 + Math.cos((i / 100) * 6.283) * 500, 1024 + Math.sin((i / 100) * 6.283) * 500]);
        s.applyPolygon(v, true);
      });
      out.undoMs = time(() => s.undo());
      out.redoMs = time(() => s.redo());
      s.fitView();
      out.renderFitMs = s.renderNow();
      s.setView(8, -8000, -8000);
      out.renderZoom8xMs = s.renderNow();
      let body;
      out.exportCompressedMs = time(() => (body = s.export(true)));
      out.exportBytesCompressed = JSON.stringify(body).length;
      out.exportUncompressedMs = time(() => (body = s.export(false)));
      out.exportBytesUncompressed = JSON.stringify(body).length;
      out.lungArea = s.area("lung");
      out.jsHeapMB = performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null;
      return out;
    });
    // One real pointer stroke at fit scale, to see the per-move cost through the event path.
    await page.evaluate(() => {
      window.spike.fitView();
      window.spike.setBrush(30);
      window.spike.setTool("brush");
    });
    const a = await page.evaluate(() => window.spike.toClient(200, 1000));
    const b = await page.evaluate(() => window.spike.toClient(1800, 1200));
    const t1 = Date.now();
    await page.mouse.move(a.x, a.y);
    await page.mouse.down();
    await page.mouse.move(b.x, b.y, { steps: 60 });
    await page.mouse.up();
    perf.realStroke60MovesWallMs = Date.now() - t1;
    const tm = await page.evaluate(() => window.spike.timings());
    perf.realStrokeDownToUpMs = tm.lastStrokeMs; // includes Playwright's per-step round trips
    perf.realStrokeHandlerMsTotal = tm.moveHandlerMsTotal; // the page's own work in pointermove
    perf.realStrokeMoveEvents = tm.moveCount;
    perf.pageLoadMs = loadMs;
    fs.writeFileSync(path.join(OUT, `perf-${tag}.json`), JSON.stringify(perf, null, 1));
    console.log("perf (2048 x 2048):", perf);
    await page.close();
  }
}

await browser.close();
server.close();
if (failures.length) {
  console.error(`\n${failures.length} browser-side check(s) failed:\n - ${failures.join("\n - ")}`);
  process.exit(1);
}
console.log("browser-side checks passed");
