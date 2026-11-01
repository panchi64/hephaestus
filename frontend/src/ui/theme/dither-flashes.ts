/**
 * Lightning, from the strikes the platform raises to what the shader draws: each live
 * strike expands into its flashes, and the flashes group into the canvas patches that
 * are redrawn with them. Everything here runs per draw, so none of it allocates.
 */
import {
  CELL,
  FLASHES,
  FLASH_MS,
  FLASH_SPREAD_MS,
  MAX_STRIKES,
  STRIKE_LIFE_MS,
  TONE_INDEX,
  type Box,
  type Strike,
} from "./dither";

/** Flicker beats: a flash is at full strength on these tenths and dim between. */
const BEAT_MS = 100;
const BRIGHT_BEATS = [0, 2, 5, 9];

/** One flash per vec4 in the shader: x, y (viewport px), spread (cells²), and tone
 *  index plus strength (always under 1) packed into one float. */
export const MAX_FLASHES = MAX_STRIKES * FLASHES;

/** A flash is invisible beyond `spread × FLASH_REACH` cells² from its centre: its
 *  strength there is under the lowest Bayer threshold. The shader culls on it too. */
export const FLASH_REACH = 3.6;

/** Ints per patch from `flashPatches`: x, y (from the canvas's bottom left), width,
 *  height, then the first flash and the flash count in the grouped buffer. */
export const PATCH = 6;

/** A deterministic 0–1 hash of a strike id and a salt. */
function hash01(id: number, salt: number): number {
  let h = Math.imul(id ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(salt, 0xc2b2ae35);
  h = Math.imul(h ^ (h >>> 16), 0x7feb352d);
  h = Math.imul(h ^ (h >>> 15), 0x846ca68b);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

/** Whether any strike is still within its life at `now`. */
export function strikesAlive(strikes: readonly Strike[], now: number): boolean {
  for (let i = 0; i < strikes.length; i++) {
    const age = now - strikes[i].at;
    if (age >= 0 && age <= STRIKE_LIFE_MS) return true;
  }
  return false;
}

/** Expands the newest `MAX_STRIKES` strikes still flashing at `now` into `out` and
 *  returns how many flashes it wrote. */
export function writeFlashes(
  strikes: readonly Strike[],
  now: number,
  out: Float32Array,
): number {
  let count = 0;
  let taken = 0;
  for (let i = strikes.length - 1; i >= 0 && taken < MAX_STRIKES; i--) {
    const s = strikes[i];
    const age = now - s.at;
    if (age < 0 || age > STRIKE_LIFE_MS) continue;
    taken++;
    for (let k = 0; k < FLASHES; k++) {
      const flashAge = age - hash01(s.id, k) * FLASH_SPREAD_MS;
      if (flashAge < 0 || flashAge > FLASH_MS) continue;
      const beat = Math.floor(flashAge / BEAT_MS);
      const flicker = BRIGHT_BEATS.includes(beat) ? 1 : 0.25;
      const strength = flicker * (1 - flashAge / FLASH_MS) * 0.85;
      const o = count * 4;
      out[o] = s.x + (hash01(s.id, k + 10) - 0.5) * 80;
      out[o + 1] = s.y + (hash01(s.id, k + 20) - 0.5) * 80;
      out[o + 2] = 60 + 140 * hash01(s.id, k + 30);
      out[o + 3] = TONE_INDEX[s.tone] + Math.min(strength, 0.999);
      count++;
    }
  }
  return count;
}

/** A flash's visible square in canvas pixels, clipped, as x0, y0, x1, y1 in `out`;
 *  false when none of it is on the canvas. */
function flashSquare(
  flashes: Float32Array,
  i: number,
  box: Box,
  out: Int32Array,
): boolean {
  const cols = box.width / CELL;
  const rows = box.height / CELL;
  const reach = Math.sqrt(flashes[i * 4 + 2] * FLASH_REACH) + 1;
  const cx = (flashes[i * 4] - box.x) / CELL;
  // Canvas rows count from the bottom.
  const cy = rows - (flashes[i * 4 + 1] - box.y) / CELL;
  out[0] = Math.max(0, Math.floor(cx - reach));
  out[1] = Math.max(0, Math.floor(cy - reach));
  out[2] = Math.min(cols, Math.ceil(cx + reach));
  out[3] = Math.min(rows, Math.ceil(cy + reach));
  return out[2] > out[0] && out[3] > out[1];
}

const square = new Int32Array(4);

/**
 * Groups the visible flashes into patches: canvas rectangles that don't touch, each
 * holding the flashes that reach into it. Writes the patches to `patches` (`PATCH`
 * ints each) and the flashes, reordered patch by patch, to `grouped`; returns the
 * patch count. Only the patches are redrawn with lightning, each with its own flashes.
 */
export function flashPatches(
  flashes: Float32Array,
  count: number,
  box: Box,
  patches: Int32Array,
  grouped: Float32Array,
): number {
  let n = 0;
  for (let i = 0; i < count; i++) {
    if (!flashSquare(flashes, i, box, square)) continue;
    let x0 = square[0];
    let y0 = square[1];
    let x1 = square[2];
    let y1 = square[3];
    // Merge into any patch it touches; start over, since a merge can reach another.
    for (let j = 0; j < n;) {
      const o = j * PATCH;
      const ox1 = patches[o] + patches[o + 2];
      const oy1 = patches[o + 1] + patches[o + 3];
      if (x0 <= ox1 && patches[o] <= x1 && y0 <= oy1 && patches[o + 1] <= y1) {
        x0 = Math.min(x0, patches[o]);
        y0 = Math.min(y0, patches[o + 1]);
        x1 = Math.max(x1, ox1);
        y1 = Math.max(y1, oy1);
        n--;
        patches.copyWithin(o, n * PATCH, n * PATCH + PATCH);
        j = 0;
      } else j++;
    }
    const o = n * PATCH;
    patches[o] = x0;
    patches[o + 1] = y0;
    patches[o + 2] = x1 - x0;
    patches[o + 3] = y1 - y0;
    n++;
  }
  // Patches don't touch, so each visible flash lies in exactly one.
  let written = 0;
  for (let j = 0; j < n; j++) {
    const o = j * PATCH;
    patches[o + 4] = written;
    for (let i = 0; i < count; i++) {
      if (!flashSquare(flashes, i, box, square)) continue;
      if (square[0] < patches[o] || square[0] >= patches[o] + patches[o + 2])
        continue;
      if (
        square[1] < patches[o + 1] ||
        square[1] >= patches[o + 1] + patches[o + 3]
      )
        continue;
      for (let k = 0; k < 4; k++) grouped[written * 4 + k] = flashes[i * 4 + k];
      written++;
    }
    patches[o + 5] = written - patches[o + 4];
  }
  return n;
}
