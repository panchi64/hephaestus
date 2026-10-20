import { describe, expect, test } from "bun:test";
import {
  GLOW_LIFE_MS,
  GLOW_REACH,
  MAX_PULSES,
  PULSE_TONES,
  livePulses,
  mixRgb,
  type Pulse,
} from "./dither";
import { FRAGMENT_SHADER } from "./dither-shader";

const pulse = (id: number, at: number): Pulse => ({
  id,
  at,
  x: 0,
  y: 0,
  tone: "info",
});

describe("livePulses", () => {
  test("drops glows that have not started or have finished", () => {
    const now = 10_000;
    const kept = livePulses(
      [
        pulse(1, now - GLOW_LIFE_MS - 1),
        pulse(2, now - 100),
        pulse(3, now + 50),
      ],
      now,
    );
    expect(kept.map((p) => p.id)).toEqual([2]);
  });

  test("keeps the newest, in arrival order", () => {
    const list = Array.from({ length: 12 }, (_, i) => pulse(i, 1000 + i));
    const kept = livePulses(list, 1100, 8);
    expect(kept.map((p) => p.id)).toEqual([4, 5, 6, 7, 8, 9, 10, 11]);
  });
});

describe("the shader", () => {
  test("takes its sizes and glow reach from the constants", () => {
    expect(FRAGMENT_SHADER).toContain(`uniform vec4 uPulses[${MAX_PULSES}]`);
    expect(FRAGMENT_SHADER).toContain(
      `uniform vec3 uPalette[${PULSE_TONES.length}]`,
    );
    expect(FRAGMENT_SHADER).toContain(GLOW_LIFE_MS.toFixed(4));
    expect(FRAGMENT_SHADER).toContain((2 * GLOW_REACH ** 2).toFixed(4));
  });

  test("decides which cells are lit before any glow is read", () => {
    expect(FRAGMENT_SHADER.indexOf("bayer4(cell)")).toBeLessThan(
      FRAGMENT_SHADER.indexOf("uPulses[i]"),
    );
  });

  test("interpolates no bare integers into float expressions", () => {
    expect(FRAGMENT_SHADER).not.toMatch(/\$\{|undefined|NaN/);
  });
});

describe("mixRgb", () => {
  test("moves only the asked fraction of the way", () => {
    expect(mixRgb([0, 0, 0], [100, 200, 50], 0.16)).toEqual([16, 32, 8]);
    expect(mixRgb([23, 23, 23], [255, 0, 0], 0)).toEqual([23, 23, 23]);
  });
});
