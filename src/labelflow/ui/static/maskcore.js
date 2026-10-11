// maskcore.js — the raster side of the mask tools, with no dependencies.
//
// A mask is a Uint8Array of height * width bytes in ROW-major order (index = y * width + x),
// one per mask class, holding 0 or 1. Everything that changes a mask goes through this module
// so the rasterization rules are in one place and are the same whichever drawing library
// draws the screen.
//
// Rasterization rules (the "pixel center" convention):
//   * Pixel (x, y) covers the square [x, x+1) x [y, y+1) of image space; its center is
//     (x + 0.5, y + 0.5). Pointer positions are continuous image coordinates, never snapped.
//   * The brush is centered on the CENTER of the pixel under the pointer (snapToCenter), so a
//     1 px brush always paints exactly that pixel and every brush dot is a symmetric disk.
//   * Brush dot at (snapped) p with diameter d: pixels whose center c has |c - p| <= d / 2.
//     For d = 11 that is the 97 pixels with integer offsets dx^2 + dy^2 <= 30.25.
//   * Brush stroke from p0 to p1: pixels whose center is within d / 2 of the segment (a
//     capsule). A stroke of n points is the union of its n - 1 capsules; a stroke of one
//     point is a dot.
//   * Polygon: pixels whose center is inside the polygon by the EVEN-ODD rule, with the
//     half-open scanline convention (an edge counts for scanline y + 0.5 when exactly one of
//     its endpoints has y <= y + 0.5; a span [xa, xb) covers the pixels whose center is >= xa
//     and < xb). An axis-aligned rectangle with integer corners therefore fills exactly
//     (x1 - x0) * (y1 - y0) pixels, and two polygons sharing an edge never overlap or leave
//     a gap along it.
//   Boundary comparisons are done with exact arithmetic where the inputs allow it (squared
//   distances compared as cross^2 <= r^2 * len^2), so a Python re-implementation of the same
//   formulas gives the same pixels; check/check_export.py relies on that.

export class Mask {
  constructor(width, height, data) {
    this.width = width;
    this.height = height;
    this.data = data || new Uint8Array(width * height);
  }

  static fromRLE(rle) {
    const [height, width] = rle.size;
    const counts = typeof rle.counts === "string" ? countsFromString(rle.counts) : rle.counts;
    const mask = new Mask(width, height);
    const data = mask.data;
    let position = 0; // position in column-major order
    let value = 0;
    for (const count of counts) {
      if (value) {
        for (let k = 0; k < count; k++) {
          const p = position + k;
          data[(p % height) * width + Math.floor(p / height)] = 1;
        }
      }
      position += count;
      value ^= 1;
    }
    if (position !== width * height) throw new Error("RLE counts do not add up to the size");
    return mask;
  }

  // COCO RLE, column-major, starting with a (possibly empty) background run.
  toRLE(compressed = true) {
    const { width, height, data } = this;
    const counts = [];
    let current = 0;
    let run = 0;
    for (let x = 0; x < width; x++) {
      for (let y = 0; y < height; y++) {
        const value = data[y * width + x] ? 1 : 0;
        if (value === current) {
          run++;
        } else {
          counts.push(run);
          current = value;
          run = 1;
        }
      }
    }
    counts.push(run);
    return { size: [height, width], counts: compressed ? countsToString(counts) : counts };
  }

  area() {
    let n = 0;
    for (let i = 0; i < this.data.length; i++) n += this.data[i];
    return n;
  }

  isEmpty() {
    return this.data.every((v) => v === 0);
  }
}

// --- COCO compressed counts (pycocotools rleToString / rleFrString) ----------------------

export function countsToString(counts) {
  let out = "";
  for (let i = 0; i < counts.length; i++) {
    let number = i > 2 ? counts[i] - counts[i - 2] : counts[i];
    let more = true;
    while (more) {
      let group = number & 0x1f;
      number >>= 5; // arithmetic shift: a negative number ends at -1
      more = group & 0x10 ? number !== -1 : number !== 0;
      if (more) group |= 0x20;
      out += String.fromCharCode(group + 48);
    }
  }
  return out;
}

export function countsFromString(text) {
  const counts = [];
  let position = 0;
  while (position < text.length) {
    let number = 0;
    let shift = 0;
    for (;;) {
      if (position >= text.length) throw new Error("counts end in the middle of a number");
      const code = text.charCodeAt(position) - 48;
      if (code < 0 || code >= 64) throw new Error("bad character in compressed counts");
      position++;
      number |= (code & 0x1f) << shift;
      shift += 5;
      if (!(code & 0x20)) {
        if (code & 0x10) number |= -1 << shift;
        break;
      }
    }
    if (counts.length > 2) number += counts[counts.length - 2];
    counts.push(number);
  }
  return counts;
}

// --- Rasterizers ---------------------------------------------------------------------------
// Each returns the dirty bounding box {x0, y0, x1, y1} (half-open) it touched, or null.

/** Paint (value 1) or erase (value 0) a disk: centers within radius of (px, py). */
export function paintDisk(mask, px, py, radius, value) {
  const { width, height, data } = mask;
  const r2 = radius * radius;
  const y0 = Math.max(0, Math.floor(py - radius - 0.5));
  const y1 = Math.min(height - 1, Math.ceil(py + radius - 0.5));
  const x0 = Math.max(0, Math.floor(px - radius - 0.5));
  const x1 = Math.min(width - 1, Math.ceil(px + radius - 0.5));
  for (let y = y0; y <= y1; y++) {
    const dy = y + 0.5 - py;
    for (let x = x0; x <= x1; x++) {
      const dx = x + 0.5 - px;
      if (dx * dx + dy * dy <= r2) data[y * width + x] = value;
    }
  }
  return box(x0, y0, x1 + 1, y1 + 1, width, height);
}

/** Paint or erase a capsule: centers within radius of the segment (ax, ay)-(bx, by). */
export function paintCapsule(mask, ax, ay, bx, by, radius, value) {
  const { width, height, data } = mask;
  const ex = bx - ax;
  const ey = by - ay;
  const len2 = ex * ex + ey * ey;
  if (len2 === 0) return paintDisk(mask, ax, ay, radius, value);
  const r2 = radius * radius;
  const y0 = Math.max(0, Math.floor(Math.min(ay, by) - radius - 0.5));
  const y1 = Math.min(height - 1, Math.ceil(Math.max(ay, by) + radius - 0.5));
  const x0 = Math.max(0, Math.floor(Math.min(ax, bx) - radius - 0.5));
  const x1 = Math.min(width - 1, Math.ceil(Math.max(ax, bx) + radius - 0.5));
  for (let y = y0; y <= y1; y++) {
    const cy = y + 0.5;
    for (let x = x0; x <= x1; x++) {
      const cx = x + 0.5;
      const dx = cx - ax;
      const dy = cy - ay;
      const dot = dx * ex + dy * ey;
      let inside;
      if (dot <= 0) {
        inside = dx * dx + dy * dy <= r2;
      } else if (dot >= len2) {
        const fx = cx - bx;
        const fy = cy - by;
        inside = fx * fx + fy * fy <= r2;
      } else {
        const cross = ex * dy - ey * dx;
        inside = cross * cross <= r2 * len2;
      }
      if (inside) data[y * width + x] = value;
    }
  }
  return box(x0, y0, x1 + 1, y1 + 1, width, height);
}

/** The center of the pixel an image-space point falls in: where the brush is centered. */
export function snapToCenter([x, y]) {
  return [Math.floor(x) + 0.5, Math.floor(y) + 0.5];
}

/** Paint or erase a polyline stroke of diameter d (dot for one point, capsules otherwise).
 * The points are snapped to pixel centers first. */
export function paintStroke(mask, rawPoints, diameter, value) {
  const radius = diameter / 2;
  const points = rawPoints.map(snapToCenter);
  if (points.length === 0) return null;
  if (points.length === 1) return paintDisk(mask, points[0][0], points[0][1], radius, value);
  let dirty = null;
  for (let i = 1; i < points.length; i++) {
    const [ax, ay] = points[i - 1];
    const [bx, by] = points[i];
    dirty = union(dirty, paintCapsule(mask, ax, ay, bx, by, radius, value));
  }
  return dirty;
}

/** Paint or erase a polygon (array of [x, y]) by the even-odd rule at pixel centers. */
export function paintPolygon(mask, vertices, value) {
  const { width, height, data } = mask;
  const n = vertices.length;
  if (n < 3) return null;
  let minY = Infinity;
  let maxY = -Infinity;
  let minX = Infinity;
  let maxX = -Infinity;
  for (const [x, y] of vertices) {
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
  }
  const rowStart = Math.max(0, Math.floor(minY - 0.5));
  const rowEnd = Math.min(height - 1, Math.ceil(maxY - 0.5));
  const crossings = [];
  for (let y = rowStart; y <= rowEnd; y++) {
    const sy = y + 0.5;
    crossings.length = 0;
    for (let i = 0; i < n; i++) {
      const [x0, y0] = vertices[i];
      const [x1, y1] = vertices[(i + 1) % n];
      if ((y0 <= sy) !== (y1 <= sy)) {
        crossings.push(x0 + ((sy - y0) * (x1 - x0)) / (y1 - y0));
      }
    }
    crossings.sort((a, b) => a - b);
    for (let k = 0; k + 1 < crossings.length; k += 2) {
      // pixels whose center x + 0.5 lies in [xa, xb)
      const xa = Math.max(0, Math.ceil(crossings[k] - 0.5));
      const xb = Math.min(width, Math.ceil(crossings[k + 1] - 0.5));
      for (let x = xa; x < xb; x++) data[y * width + x] = value;
    }
  }
  return box(
    Math.floor(minX - 0.5),
    rowStart,
    Math.ceil(maxX - 0.5) + 1,
    rowEnd + 1,
    width,
    height,
  );
}

function box(x0, y0, x1, y1, width, height) {
  x0 = Math.max(0, x0);
  y0 = Math.max(0, y0);
  x1 = Math.min(width, x1);
  y1 = Math.min(height, y1);
  if (x1 <= x0 || y1 <= y0) return null;
  return { x0, y0, x1, y1 };
}

export function union(a, b) {
  if (!a) return b;
  if (!b) return a;
  return {
    x0: Math.min(a.x0, b.x0),
    y0: Math.min(a.y0, b.y0),
    x1: Math.max(a.x1, b.x1),
    y1: Math.max(a.y1, b.y1),
  };
}

// --- The label being edited: one mask per class, with undo ----------------------------------

export class MaskSet {
  constructor(classNames, width, height) {
    this.width = width;
    this.height = height;
    this.masks = new Map(classNames.map((name) => [name, new Mask(width, height)]));
    this.undoStack = [];
    this.redoStack = [];
    this.maxUndo = 50;
    this._editing = null;
    // Called when a group step (replaceAll) is undone or redone, with the state the step
    // recorded besides the masks (`meta`); returns the state to record for the way back.
    this.swapMeta = null;
  }

  mask(name) {
    return this.masks.get(name);
  }

  /** Begin an undoable edit of one class. `before` is a scratch copy of the whole mask. */
  beginEdit(name) {
    const mask = this.mask(name);
    this._editing = { name, before: mask.data.slice(), dirty: null };
    return mask;
  }

  touched(dirty) {
    if (this._editing) this._editing.dirty = union(this._editing.dirty, dirty);
  }

  /** Finish the edit; keeps only the pixels in the dirty box, so undo memory stays small. */
  endEdit() {
    const edit = this._editing;
    this._editing = null;
    if (!edit || !edit.dirty) return null;
    const entry = { name: edit.name, box: edit.dirty, before: crop(edit.before, this.width, edit.dirty) };
    this.undoStack.push(entry);
    if (this.undoStack.length > this.maxUndo) this.undoStack.shift();
    this.redoStack.length = 0;
    return edit.dirty;
  }

  /**
   * Replace every class's mask in one undoable step. `datas` maps class name to the new
   * data (a Uint8Array of the same size; a class left out becomes empty). `meta` is what the
   * caller wants restored on undo besides the masks (see swapMeta). Returns the changed
   * classes as [{name, box}]; a class whose pixels did not change is not in the list.
   */
  replaceAll(datas, meta = null) {
    const parts = [];
    for (const [name, mask] of this.masks) {
      const next = datas.get(name) || new Uint8Array(mask.data.length);
      const box = diffBox(mask.data, next, this.width, this.height);
      if (!box) continue;
      parts.push({ name, box, before: crop(mask.data, this.width, box) });
      mask.data.set(next);
    }
    this.undoStack.push({ parts, meta });
    if (this.undoStack.length > this.maxUndo) this.undoStack.shift();
    this.redoStack.length = 0;
    return parts.map(({ name, box }) => ({ name, box }));
  }

  undo() {
    return this._swap(this.undoStack, this.redoStack);
  }

  redo() {
    return this._swap(this.redoStack, this.undoStack);
  }

  /** Undo or redo one step: {name, box} for a one-class edit, {changes: [{name, box}]}
   * for a group step. */
  _swap(from, to) {
    const entry = from.pop();
    if (!entry) return null;
    if (entry.parts) {
      const parts = entry.parts.map((part) => this._swapPart(part));
      const meta = this.swapMeta && entry.meta ? this.swapMeta(entry.meta) : entry.meta;
      to.push({ parts, meta });
      return { changes: parts.map(({ name, box }) => ({ name, box })) };
    }
    const part = this._swapPart(entry);
    to.push(part);
    return { name: part.name, box: part.box };
  }

  _swapPart({ name, box, before }) {
    const mask = this.mask(name);
    const now = crop(mask.data, this.width, box);
    paste(mask.data, this.width, box, before);
    return { name, box, before: now };
  }

  toLabelMasks(compressed = true) {
    const out = {};
    for (const [name, mask] of this.masks) out[name] = mask.toRLE(compressed);
    return out;
  }
}

/** The smallest box holding every pixel where `a` and `b` differ, or null if none does. */
function diffBox(a, b, width, height) {
  let box = null;
  for (let y = 0; y < height; y++) {
    const row = y * width;
    let x0 = -1;
    let x1 = -1;
    for (let x = 0; x < width; x++) {
      if (a[row + x] !== b[row + x]) {
        if (x0 < 0) x0 = x;
        x1 = x;
      }
    }
    if (x0 >= 0) box = union(box, { x0, y0: y, x1: x1 + 1, y1: y + 1 });
  }
  return box;
}

function crop(data, width, b) {
  const w = b.x1 - b.x0;
  const out = new Uint8Array(w * (b.y1 - b.y0));
  for (let y = b.y0; y < b.y1; y++) {
    out.set(data.subarray(y * width + b.x0, y * width + b.x1), (y - b.y0) * w);
  }
  return out;
}

function paste(data, width, b, patch) {
  const w = b.x1 - b.x0;
  for (let y = b.y0; y < b.y1; y++) {
    data.set(patch.subarray((y - b.y0) * w, (y - b.y0 + 1) * w), y * width + b.x0);
  }
}

// --- Drawing a mask into an RGBA canvas (the display side) --------------------------------

/** Keep an offscreen canvas of the mask's size tinted with one color; update a box of it. */
export class MaskCanvas {
  constructor(mask, rgb, alpha = 150) {
    this.mask = mask;
    this.rgb = rgb;
    this.alpha = alpha;
    this.canvas = document.createElement("canvas");
    this.canvas.width = mask.width;
    this.canvas.height = mask.height;
    this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
    this.refresh(null);
  }

  refresh(b) {
    const { width, height, data } = this.mask;
    const region = b || { x0: 0, y0: 0, x1: width, y1: height };
    const w = region.x1 - region.x0;
    const h = region.y1 - region.y0;
    if (w <= 0 || h <= 0) return;
    const image = this.ctx.createImageData(w, h);
    const px = image.data;
    const [r, g, bl] = this.rgb;
    for (let y = 0; y < h; y++) {
      const row = (region.y0 + y) * width + region.x0;
      for (let x = 0; x < w; x++) {
        if (data[row + x]) {
          const i = (y * w + x) * 4;
          px[i] = r;
          px[i + 1] = g;
          px[i + 2] = bl;
          px[i + 3] = this.alpha;
        }
      }
    }
    this.ctx.putImageData(image, region.x0, region.y0);
  }
}
