import type { JSX } from "solid-js";

/** The View panel's zoom scale (-2..+2), shared by every renderer so the same
 *  step reads as the same size everywhere. Anchored to the app's real type-scale
 *  steps (micro 10/12, label 11/16, body 13/16, readout 20/24, readout-lg 32/36).
 *  Both forms — the inline style (`fontStepStyle`, for text laid out in normal
 *  flow) and the numeric px (`fontStepMetrics`, for virtualization row-height
 *  math) — resolve to the identical values, so switching between e.g. a JSON
 *  artifact and a raw log at the same zoom step never jumps size.
 *
 *  An inline style rather than a size class because the viewers set their text in
 *  `Text variant="code"`, which carries a size class of its own: two size classes
 *  on one node resolve by stylesheet order, an inline style always wins. */

const FONT_STEP_METRICS: ReadonlyArray<{ size: number; line: number }> = [
  { size: 10, line: 12 },
  { size: 11, line: 16 },
  { size: 13, line: 16 },
  { size: 20, line: 24 },
  { size: 32, line: 36 },
];

function clampStep(step: number | undefined): number {
  return Math.max(-2, Math.min(2, step ?? 0));
}

/** The scale as raw px `{size, line}` — for renderers that need a number
 *  (RawTextViewer's virtualization row height). */
export function fontStepMetrics(step: number | undefined): {
  size: number;
  line: number;
} {
  return FONT_STEP_METRICS[clampStep(step) + 2];
}

/** The same scale as an inline `font-size` + `line-height`. */
export function fontStepStyle(step: number | undefined): JSX.CSSProperties {
  const m = fontStepMetrics(step);
  return { "font-size": `${m.size}px`, "line-height": `${m.line}px` };
}

/** The zoom factor for `step` applied to a whole framed page (`SandboxedFrame`).
 *  A sandboxed iframe is opaque-origin, so no class or font-size can reach the
 *  document inside — the only lever is scaling the page like browser zoom. Uses
 *  browser-zoom-style factors rather than the text metrics' ratios (10px→32px
 *  would swing a full page between 77% and 246%, far past useful). */
const FRAME_ZOOM: ReadonlyArray<number> = [0.67, 0.8, 1, 1.25, 1.5];

export function frameZoom(step: number | undefined): number {
  return FRAME_ZOOM[clampStep(step) + 2];
}
