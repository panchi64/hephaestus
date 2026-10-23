import { describe, expect, test } from "bun:test";
import {
  FLASH_MS,
  LIGHT,
  LOAD_CAP,
  MAX_STRIKES,
  SPIN,
  STRIKE_LIFE_MS,
  STRIKE_TONES,
  advancePhase,
  liveStrikes,
  mixRgb,
  planetDisc,
  strikePoint,
  type Strike,
} from "./dither";
import { FRAGMENT_SHADER } from "./dither-shader";

const strike = (id: number, at: number): Strike => ({
  id,
  at,
  x: 0,
  y: 0,
  tone: "info",
});

/** A deterministic stand-in for `Math.random`. */
function seeded(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

describe("planetDisc", () => {
  test("sits low in the bottom-right corner, sized by height", () => {
    const { cx, cy, r } = planetDisc(1600, 1000);
    expect(r).toBe(720);
    expect(cx).toBeCloseTo(1600 - 0.54 * 720);
    expect(cy).toBe(920);
  });

  test("is sized by width on a narrow screen", () => {
    expect(planetDisc(390, 844).r).toBeCloseTo(351);
  });
});

describe("strikePoint", () => {
  test.each([
    [1600, 1000],
    [390, 844],
    [2560, 1080],
  ])("lands on the visible night side at %ix%i", (w, h) => {
    const random = seeded(w + h);
    const { cx, cy, r } = planetDisc(w, h);
    for (let i = 0; i < 200; i++) {
      const { x, y } = strikePoint(w, h, random);
      expect(x).toBeGreaterThan(0);
      expect(x).toBeLessThan(w);
      expect(y).toBeGreaterThan(0);
      expect(y).toBeLessThan(h);
      const nx = (x - cx) / r;
      const ny = (y - cy) / r;
      const r2 = nx * nx + ny * ny;
      expect(r2).toBeLessThan(1);
      const lit = nx * LIGHT[0] + ny * LIGHT[1] + Math.sqrt(1 - r2) * LIGHT[2];
      expect(lit).toBeLessThan(0.05);
    }
  });
});

describe("advancePhase", () => {
  test("turns at the resting rate with nothing running", () => {
    expect(advancePhase(1, 1000, 0)).toBeCloseTo(1 + SPIN);
  });

  test("turns twice as fast at the load cap, and no faster past it", () => {
    expect(advancePhase(0, 1000, LOAD_CAP)).toBeCloseTo(2 * SPIN);
    expect(advancePhase(0, 1000, LOAD_CAP + 3)).toBeCloseTo(2 * SPIN);
  });
});

describe("liveStrikes", () => {
  test("drops strikes that have not started or have finished", () => {
    const now = 10_000;
    const kept = liveStrikes(
      [
        strike(1, now - STRIKE_LIFE_MS - 1),
        strike(2, now - 100),
        strike(3, now + 50),
      ],
      now,
    );
    expect(kept.map((s) => s.id)).toEqual([2]);
  });

  test("keeps the newest, in arrival order", () => {
    const list = Array.from({ length: 12 }, (_, i) => strike(i, 1000 + i));
    const kept = liveStrikes(list, 1100, 8);
    expect(kept.map((s) => s.id)).toEqual([4, 5, 6, 7, 8, 9, 10, 11]);
  });
});

describe("the shader", () => {
  test("takes its sizes and timings from the constants", () => {
    expect(FRAGMENT_SHADER).toContain(`uniform vec4 uStrikes[${MAX_STRIKES}]`);
    expect(FRAGMENT_SHADER).toContain(
      `uniform vec3 uPalette[${STRIKE_TONES.length}]`,
    );
    expect(FRAGMENT_SHADER).toContain(STRIKE_LIFE_MS.toFixed(4));
    expect(FRAGMENT_SHADER).toContain(FLASH_MS.toFixed(4));
    expect(FRAGMENT_SHADER).toContain(LIGHT[0].toFixed(4));
  });

  test("leaves the ground plain before shading any cloud", () => {
    expect(FRAGMENT_SHADER.indexOf("if (r > 1.2)")).toBeLessThan(
      FRAGMENT_SHADER.indexOf("clouds(n)"),
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
