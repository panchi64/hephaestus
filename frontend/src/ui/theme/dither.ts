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
 *  viewer). It puts the terminator on the visible disc with night on the right. */
export const LIGHT: readonly [number, number, number] = (() => {
  const v = [-1, -0.3, 0.12];
  const l = Math.hypot(...v);
  return [v[0] / l, v[1] / l, v[2] / l];
})();

/** Strikes land where the surface faces the sun less than this. */
const NIGHT = 0.05;
/** Strikes keep this far (CSS px) inside the viewport. */
const MARGIN = 8;

export interface Disc {
  cx: number;
  cy: number;
  r: number;
}

/** The planet in viewport CSS px: low in the bottom-right corner, sized by height,
 *  and by width on a narrow screen. */
export function planetDisc(width: number, height: number): Disc {
  const r = Math.min(0.72 * height, 0.9 * width);
  return { cx: width - 0.54 * r, cy: 0.92 * height, r };
}

/** A random point on the visible night side, for an event's lightning. */
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
    const r2 = nx * nx + ny * ny;
    if (r2 >= 1) continue;
    const lit = nx * LIGHT[0] + ny * LIGHT[1] + Math.sqrt(1 - r2) * LIGHT[2];
    if (lit < NIGHT) return { x, y };
  }
  return {
    x: Math.min(cx + 0.4 * r, width - MARGIN),
    y: Math.max(cy - 0.4 * r, MARGIN),
  };
}

/** The strikes still flashing at `now`, newest last, at most `max` of them. */
export function liveStrikes(
  strikes: readonly Strike[],
  now: number,
  max: number = MAX_STRIKES,
): Strike[] {
  const live = strikes.filter(
    (s) => now >= s.at && now - s.at <= STRIKE_LIFE_MS,
  );
  return live.slice(-max);
}

export type Rgb = readonly [number, number, number];

/** `a` moved `f` of the way to `b`, per channel. */
export function mixRgb(a: Rgb, b: Rgb, f: number): Rgb {
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}
