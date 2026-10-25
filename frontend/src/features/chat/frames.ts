/** The outline a work-log glyph sits in, which says what kind of thing the row is.
 *
 *  `square` is work: a tool call, a building block. `diamond` is context: something
 *  read into the model, or (dashed) put in front of it by the chassis. `circle` is the
 *  model's own reasoning. `octagon` is a checkpoint: the Auto review. `out` and `in` are
 *  traffic with a sub-agent: a brief or message sent to one, and a report back. */
export type Frame = "square" | "diamond" | "circle" | "octagon" | "out" | "in";

export interface FrameGeometry {
  /** The outline, in the socket's 24×24 drawing box, centred on (12, 12). */
  d: string;
  /** Where the outline meets the row's centre line on its left, so a lead can run from
   *  the elbow into it. */
  left: number;
}

/** Every frame shares one centre so the glyphs stand in one column whatever surrounds
 *  them, and each is sized to leave the glyph about the same clearance. */
export const FRAMES: Record<Frame, FrameGeometry> = {
  square: { d: "M4.5 4.5H19.5V19.5H4.5Z", left: 4.5 },
  octagon: {
    d: "M8.5 4.5H15.5L19.5 8.5V15.5L15.5 19.5H8.5L4.5 15.5V8.5Z",
    left: 4.5,
  },
  circle: { d: "M3.5 12a8.5 8.5 0 1 0 17 0a8.5 8.5 0 1 0-17 0Z", left: 3.5 },
  diamond: { d: "M1 12L12 1L23 12L12 23Z", left: 1 },
  out: { d: "M4.5 4.5H19.5V8.5L23 12L19.5 15.5V19.5H4.5Z", left: 4.5 },
  in: { d: "M19.5 4.5H4.5V8.5L1 12L4.5 15.5V19.5H19.5Z", left: 1 },
};
