/**
 * The ambient field's constants (§11.1). The field itself is drawn by the fragment
 * shader in `dither-shader.ts`, which takes every number below by interpolation, so
 * this file is the one place they are set.
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

/** One event's glow: where it started (CSS px, viewport) and when (`performance.now()`). */
export interface Pulse {
  id: number;
  at: number;
  x: number;
  y: number;
  tone: PulseTone;
}

/** Density: a base, a slow drift on top, and one step per live run up to the cap.
 *  No cell is ever more likely lit than `DENSITY_MAX`. */
export const BASE = 0.24;
export const DRIFT = 0.12;
export const PER_LOAD = 0.04;
export const LOAD_CAP = 5;
export const DENSITY_MAX = 0.6;

/** An event's glow: a still patch around the source whose lit cells lean toward the
 *  event's hue, never denser. Life and ease-in (ms), and the patch's gaussian sigma
 *  (CSS px). It fades linearly from the end of the rise to the end of its life. */
export const GLOW_LIFE_MS = 5000;
export const GLOW_RISE_MS = 600;
export const GLOW_REACH = 120;

/** How many glows the shader takes at once. */
export const MAX_PULSES = 8;

/** The glows still alive at `now`, newest last, at most `max` of them. */
export function livePulses(
  pulses: readonly Pulse[],
  now: number,
  max: number = MAX_PULSES,
): Pulse[] {
  const live = pulses.filter((p) => now >= p.at && now - p.at <= GLOW_LIFE_MS);
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
