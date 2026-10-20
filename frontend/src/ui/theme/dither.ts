/**
 * The ambient field's math (§11.1): an ordered-dither (4×4 Bayer) density field that
 * drifts slowly, steps denser with platform load and carries a ring out from where an
 * event happened. Pure, so the canvas in `DitherField` only paints what this decides.
 *
 * Coordinates are field cells; time is seconds for the drift and milliseconds for
 * pulses, which carry a `performance.now()` stamp.
 */

/** CSS px per field cell: half the 4px base unit (§3), fine enough that the dither
 *  reads as a tone rather than as dots. */
export const CELL = 2;

/** What a pulse's lit cells lean toward. `neutral` leans nowhere. */
export const PULSE_TONES = [
  "neutral",
  "info",
  "nominal",
  "warn",
  "alert",
] as const;
export type PulseTone = (typeof PULSE_TONES)[number];

/** One event's ring: where it started (CSS px, viewport) and when (`performance.now()`). */
export interface Pulse {
  id: number;
  at: number;
  x: number;
  y: number;
  tone: PulseTone;
}

const BAYER_4 = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map(
  (v) => (v + 0.5) / 16,
);

/** The ordered-dither threshold for a cell, in (0, 1). */
export function threshold(x: number, y: number): number {
  return BAYER_4[(y & 3) * 4 + (x & 3)];
}

/** Slow value noise in [0, 1]. Low spatial frequency, so it reads as weather, not grain. */
export function drift(x: number, y: number, t: number): number {
  const a = Math.sin(x * 0.016 + t * 0.07) * Math.sin(y * 0.021 - t * 0.05);
  const b = Math.sin((x + y) * 0.0095 + t * 0.035);
  return 0.5 + 0.5 * (0.6 * a + 0.4 * b);
}

/** Load stops adding density past this many live runs. */
export const LOAD_CAP = 5;
const BASE = 0.24;
const DRIFT = 0.12;
const PER_LOAD = 0.04;
/** No cell is ever more likely lit than this, however busy the platform is. */
export const DENSITY_MAX = 0.6;

/** How likely a cell is to be lit before any ring. `load` is floored, so a change in
 *  load lands as one step (machine register), never a fade. */
export function density(x: number, y: number, t: number, load: number): number {
  const steps = Math.min(Math.max(Math.floor(load), 0), LOAD_CAP);
  return Math.min(
    BASE + DRIFT * drift(x, y, t) + PER_LOAD * steps,
    DENSITY_MAX,
  );
}

/** A ring's life in ms, how fast it travels (CSS px/s), its band's spread (px) and the
 *  density it adds at its crest. */
export const RING_LIFE_MS = 2400;
const RING_SPEED = 480;
const RING_SPREAD = 18;
const RING_AMP = 0.25;

/** The density a pulse's ring adds at a point (CSS px) at `now`: a band travelling out
 *  from the source and fading to nothing over its life. */
export function ring(
  px: number,
  py: number,
  now: number,
  pulse: Pulse,
): number {
  const age = now - pulse.at;
  if (age < 0 || age > RING_LIFE_MS) return 0;
  const d = Math.hypot(px - pulse.x, py - pulse.y);
  const off = d - (age / 1000) * RING_SPEED;
  if (Math.abs(off) > RING_SPREAD * 3) return 0;
  return (
    RING_AMP *
    (1 - age / RING_LIFE_MS) *
    Math.exp(-(off * off) / (2 * RING_SPREAD * RING_SPREAD))
  );
}

/** A ring weaker than this leaves its cells neutral, so only the crest carries a hue. */
const TONE_FLOOR = 0.05;

/** The cell's paint: `-1` unlit, otherwise an index into `PULSE_TONES`. */
export function shade(
  x: number,
  y: number,
  t: number,
  now: number,
  load: number,
  pulses: readonly Pulse[],
): number {
  let v = density(x, y, t, load);
  let tone = 0;
  let strongest = TONE_FLOOR;
  if (pulses.length > 0) {
    const px = x * CELL + CELL / 2;
    const py = y * CELL + CELL / 2;
    for (const p of pulses) {
      const r = ring(px, py, now, p);
      v += r;
      if (r > strongest) {
        strongest = r;
        tone = PULSE_TONES.indexOf(p.tone);
      }
    }
  }
  return v > threshold(x, y) ? tone : -1;
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
