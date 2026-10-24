/**
 * The ambient field's constants and geometry (§11.1). The field is a dithered gas
 * giant drawn by the fragment shader in `dither-shader.ts`, which takes every number
 * below by interpolation, so this file is the one place they are set.
 */

/** CSS px per field cell: half the 4px base unit (§3). */
export const CELL = 2;

/** What a strike's flashes lean toward. `neutral` leans nowhere. */
export const STRIKE_TONES = [
  "neutral",
  "info",
  "nominal",
  "warn",
  "alert",
] as const;
export type StrikeTone = (typeof STRIKE_TONES)[number];

/** The tone the aurora leans toward. */
export const AURORA_TONE: StrikeTone = "nominal";

/** One event's lightning: where on the night side (CSS px, viewport) and when
 *  (`performance.now()`). */
export interface Strike {
  id: number;
  at: number;
  x: number;
  y: number;
  tone: StrikeTone;
}

/** Live runs past this add no more churn. */
export const LOAD_CAP = 5;
/** Live sub-agents past this add no more aurora curtains. */
export const AURORA_MAX = 6;
/** The first curtain's angle on the limb (rad, 0 to the right, negative upward); the
 *  rest follow 0.16 rad apart toward the pole, which sits on the viewport's right
 *  edge, so all of them stay on screen. */
export const AURORA_FROM = -2.5;

/** One strike is `FLASHES` flashes, each `FLASH_MS` long, starting up to
 *  `FLASH_SPREAD_MS` apart; the whole strike is over by `STRIKE_LIFE_MS`. */
export const FLASHES = 5;
export const FLASH_MS = 1400;
export const FLASH_SPREAD_MS = 900;
export const STRIKE_LIFE_MS = FLASH_SPREAD_MS + FLASH_MS;
/** How many strikes the shader takes at once. */
export const MAX_STRIKES = 8;

/** Rotation at rest (rad/s); each live run adds `CHURN_STEP` of it again, so a
 *  platform at `LOAD_CAP` turns twice as fast. */
export const SPIN = 0.004;
export const CHURN_STEP = 0.2;

/** The rotation phase after `dt` ms at a given load. Integrated per frame rather than
 *  taken as `time × speed`, so a change of speed doesn't jump the clouds. */
export function advancePhase(phase: number, dt: number, load: number): number {
  const steps = Math.min(Math.max(Math.floor(load), 0), LOAD_CAP);
  return phase + (dt / 1000) * SPIN * (1 + CHURN_STEP * steps);
}

/** Unit vector toward the sun, in the disc's frame (x right, y down, z toward the
 *  viewer): raking from the left. Only the disc's upper-left quarter is ever on
 *  screen, so all of it is day and the terminator stays past the right edge. */
export const LIGHT: readonly [number, number, number] = (() => {
  const v = [-1, -0.3, 0.12];
  const l = Math.hypot(...v);
  return [v[0] / l, v[1] / l, v[2] / l];
})();

/** Strikes land within this much of the radius (squared), off the foreshortened limb. */
const STRIKE_REACH = 0.85;
/** Strikes keep this far (CSS px) inside the viewport. */
const MARGIN = 8;

export interface Disc {
  cx: number;
  cy: number;
  r: number;
}

/** The planet in viewport CSS px: its centre on the right edge, a quarter radius
 *  below the bottom, so only its upper-left shoulder shows in the corner. Sized by
 *  height, and by width on a narrow screen. */
export function planetDisc(width: number, height: number): Disc {
  const r = Math.min(0.96 * height, 1.2 * width);
  return { cx: width, cy: height + 0.25 * r, r };
}

/** A random point on the visible disc, for an event's lightning. */
export function strikePoint(
  width: number,
  height: number,
  random: () => number = Math.random,
): { x: number; y: number } {
  const { cx, cy, r } = planetDisc(width, height);
  for (let i = 0; i < 64; i++) {
    const x = cx + (random() * 2 - 1) * r;
    const y = cy + (random() * 2 - 1) * r;
    if (x < MARGIN || x > width - MARGIN || y < MARGIN || y > height - MARGIN)
      continue;
    const nx = (x - cx) / r;
    const ny = (y - cy) / r;
    if (nx * nx + ny * ny < STRIKE_REACH) return { x, y };
  }
  return {
    x: Math.max(cx - 0.4 * r, MARGIN),
    y: Math.max(cy - 0.5 * r, MARGIN),
  };
}

/** The canvas's box in viewport CSS px: the disc and its halo, clipped to the
 *  viewport, on the cell grid so the Bayer pattern doesn't move with the box. */
export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function planetBox(width: number, height: number): Box {
  const { cx, cy, r } = planetDisc(width, height);
  const reach = HALO * r;
  const snap = (v: number) => Math.floor(v / CELL) * CELL;
  const x = Math.max(0, snap(cx - reach));
  const y = Math.max(0, snap(cy - reach));
  const right = Math.min(width, cx + reach);
  const bottom = Math.min(height, cy + reach);
  return {
    x,
    y,
    width: Math.max(CELL, Math.ceil((right - x) / CELL) * CELL),
    height: Math.max(CELL, Math.ceil((bottom - y) / CELL) * CELL),
  };
}

/** How far past the limb (in radii) the halo and aurora reach. */
export const HALO = 1.2;

/** Zonal shear speeds the fastest band to this multiple of the rotation. */
export const SHEAR = 0.3;

/** Milliseconds until the clouds at the disc's centre have moved one cell at the mean
 *  rotation. The shear averages out across the bands, so the fastest steps a little
 *  over a cell and the slowest a little under; no frame repeats the last. */
export function cloudInterval(r: number, load: number): number {
  const perMs = advancePhase(0, 1, load);
  return 1 / (perMs * (r / CELL));
}

/** The cloud deck is baked into a texture in band space (rotated longitude by
 *  latitude) and only looked up per frame. `BAKE_ROWS` rows bake per draw. */
export const BAKE_W = 2048;
export const BAKE_H = 1024;
export const STILL_BAKE_W = 512;
export const STILL_BAKE_H = 256;
export const BAKE_ROWS = 8;
/** The first bake has nothing to show until it ends, so it takes bigger strips. */
export const FIRST_BAKE_ROWS = 128;
/** Each new bake is this much rotation phase ahead of the last; the field crossfades
 *  over the same span. */
export const BAKE_STEP = 0.15;
/** The bake phase wraps here, so the noise lattice stays within float precision. */
export const BAKE_WRAP = 100;

export const TONE_INDEX: Record<StrikeTone, number> = {
  neutral: 0,
  info: 1,
  nominal: 2,
  warn: 3,
  alert: 4,
};

export type Rgb = readonly [number, number, number];

/** `a` moved `f` of the way to `b`, per channel. */
export function mixRgb(a: Rgb, b: Rgb, f: number): Rgb {
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}
