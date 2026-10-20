import { describe, expect, test } from "bun:test";
import {
  DENSITY_MAX,
  LOAD_CAP,
  RING_LIFE_MS,
  density,
  mixRgb,
  ring,
  shade,
  threshold,
  type Pulse,
} from "./dither";

const pulse = (over: Partial<Pulse> = {}): Pulse => ({
  id: 1,
  at: 0,
  x: 0,
  y: 0,
  tone: "info",
  ...over,
});

describe("threshold", () => {
  test("a 4×4 tile holds sixteen distinct levels inside (0, 1)", () => {
    const levels = new Set<number>();
    for (let y = 0; y < 4; y++)
      for (let x = 0; x < 4; x++) levels.add(threshold(x, y));
    expect(levels.size).toBe(16);
    for (const v of levels) {
      expect(v).toBeGreaterThan(0);
      expect(v).toBeLessThan(1);
    }
  });

  test("tiles repeat", () => {
    expect(threshold(5, 6)).toBe(threshold(1, 2));
  });
});

describe("density", () => {
  test("rises one step per live run and stops at the cap", () => {
    const at = (load: number) => density(10, 10, 0, load);
    expect(at(1)).toBeGreaterThan(at(0));
    expect(at(LOAD_CAP + 3)).toBe(at(LOAD_CAP));
  });

  test("a fractional load lands on the step below it", () => {
    expect(density(3, 4, 0, 2.9)).toBe(density(3, 4, 0, 2));
  });

  test("never passes the ceiling", () => {
    for (let x = 0; x < 200; x += 7)
      expect(density(x, x, x, 99)).toBeLessThanOrEqual(DENSITY_MAX);
  });
});

describe("ring", () => {
  test("is zero before it starts and once its life is over", () => {
    expect(ring(0, 0, -1, pulse())).toBe(0);
    expect(ring(0, 0, RING_LIFE_MS + 1, pulse())).toBe(0);
  });

  test("is zero far from its band", () => {
    expect(ring(2000, 0, 100, pulse())).toBe(0);
  });

  test("peaks on its band, which travels outward", () => {
    const early = ring(48, 0, 100, pulse());
    expect(early).toBeGreaterThan(0);
    expect(ring(48, 0, 1000, pulse())).toBe(0);
    expect(ring(480, 0, 1000, pulse())).toBeGreaterThan(0);
  });
});

describe("shade", () => {
  test("an unlit cell stays unlit with no pulses and no load", () => {
    // threshold(1, 2) is the tile's top level, above any resting density.
    expect(shade(1, 2, 0, 0, 0, [])).toBe(-1);
  });

  test("a lit cell on a ring's crest takes the ring's tone", () => {
    const p = pulse({ tone: "alert" });
    // Cell (24, 0) is centred at x = 49px, on the band at 100ms (48px).
    expect(shade(24, 0, 0, 100, 0, [p])).toBe(4);
  });
});

describe("mixRgb", () => {
  test("moves only the asked fraction of the way", () => {
    expect(mixRgb([0, 0, 0], [100, 200, 50], 0.16)).toEqual([16, 32, 8]);
    expect(mixRgb([23, 23, 23], [255, 0, 0], 0)).toEqual([23, 23, 23]);
  });
});
