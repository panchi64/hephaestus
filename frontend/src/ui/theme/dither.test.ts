import { describe, expect, test } from "bun:test";
import {
  AURORA_FROM,
  AURORA_MAX,
  CELL,
  FLASHES,
  HALO,
  LIGHT,
  LOAD_CAP,
  SHEAR,
  SPIN,
  STRIKE_LIFE_MS,
  STRIKE_TONES,
  TONE_INDEX,
  advancePhase,
  cloudInterval,
  mixRgb,
  planetBox,
  planetDisc,
  strikePoint,
  type Strike,
} from "./dither";
import {
  FLASH_REACH,
  MAX_FLASHES,
  PATCH,
  flashPatches,
  strikesAlive,
  writeFlashes,
} from "./dither-flashes";
import { BAKE_SHADER, FIELD_SHADER, VERTEX_SHADER } from "./dither-shader";

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
  ])("lands on the visible disc, off the limb, at %ix%i", (w, h) => {
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
      expect(nx * nx + ny * ny).toBeLessThan(0.9);
    }
  });
});

describe("the light", () => {
  test.each([
    [1600, 1000],
    [390, 844],
    [2560, 1080],
    [3840, 2160],
  ])("leaves no night side on screen at %ix%i", (w, h) => {
    const { cx, cy, r } = planetDisc(w, h);
    for (let x = 0; x < w; x += 8) {
      for (let y = 0; y < h; y += 8) {
        const nx = (x - cx) / r;
        const ny = (y - cy) / r;
        const r2 = nx * nx + ny * ny;
        if (r2 >= 1) continue;
        const lit =
          nx * LIGHT[0] + ny * LIGHT[1] + Math.sqrt(1 - r2) * LIGHT[2];
        expect(lit).toBeGreaterThan(0.1);
      }
    }
  });
});

describe("the aurora", () => {
  test.each([
    [1600, 1000],
    [390, 844],
    [3840, 2160],
  ])("keeps every curtain on screen at %ix%i", (w, h) => {
    const { cx, cy, r } = planetDisc(w, h);
    for (let k = 0; k < AURORA_MAX; k++) {
      const a = AURORA_FROM + 0.16 * k;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      expect(x).toBeGreaterThan(0);
      expect(x).toBeLessThan(w);
      expect(y).toBeGreaterThan(0);
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

describe("planetBox", () => {
  test.each([
    [1600, 1000],
    [390, 844],
    [2561, 1081],
  ])("covers the disc and halo inside the viewport at %ix%i", (w, h) => {
    const box = planetBox(w, h);
    const { cx, cy, r } = planetDisc(w, h);
    for (const v of [box.x, box.y, box.width, box.height])
      expect(v % CELL).toBe(0);
    expect(box.x).toBeLessThanOrEqual(Math.max(0, cx - HALO * r));
    expect(box.y).toBeLessThanOrEqual(Math.max(0, cy - HALO * r));
    expect(box.x + box.width).toBeGreaterThanOrEqual(w);
    expect(box.x + box.width).toBeLessThan(w + CELL);
    expect(box.y + box.height).toBeGreaterThanOrEqual(h);
    expect(box.y + box.height).toBeLessThan(h + CELL);
  });

  test("is smaller than the viewport on a desktop screen", () => {
    const box = planetBox(1600, 1000);
    expect(box.width * box.height).toBeLessThan(0.8 * 1600 * 1000);
  });
});

describe("cloudInterval", () => {
  test("waits for one cell of motion at the fastest band", () => {
    const r = 720;
    const ms = cloudInterval(r, 0);
    const moved = (SPIN / 1000) * ms * (1 + SHEAR) * (r / CELL);
    expect(moved).toBeCloseTo(1);
  });

  test("halves at the load cap", () => {
    expect(cloudInterval(720, LOAD_CAP)).toBeCloseTo(cloudInterval(720, 0) / 2);
  });
});

describe("strikesAlive", () => {
  test("is true only while a strike is within its life", () => {
    expect(strikesAlive([strike(1, 0)], STRIKE_LIFE_MS)).toBe(true);
    expect(strikesAlive([strike(1, 0)], STRIKE_LIFE_MS + 1)).toBe(false);
    expect(strikesAlive([strike(1, 100)], 50)).toBe(false);
    expect(strikesAlive([], 0)).toBe(false);
  });
});

describe("writeFlashes", () => {
  const out = new Float32Array(MAX_FLASHES * 4);

  test("writes nothing for strikes not started or finished", () => {
    const now = 10_000;
    const list = [strike(1, now - STRIKE_LIFE_MS - 1), strike(2, now + 50)];
    expect(writeFlashes(list, now, out)).toBe(0);
  });

  test("lights every flash of a strike at some point in its life", () => {
    const lit = new Set<number>();
    for (let t = 0; t <= STRIKE_LIFE_MS; t += 10) {
      const n = writeFlashes([strike(7, 0)], t, out);
      for (let i = 0; i < n; i++) lit.add(Math.round(out[i * 4 + 2]));
    }
    expect(lit.size).toBe(FLASHES);
  });

  test("packs the tone and a strength under 1", () => {
    const at = 0;
    const s: Strike = { id: 3, at, x: 500, y: 400, tone: "alert" };
    let seen = 0;
    for (let t = 0; t <= STRIKE_LIFE_MS; t += 25) {
      const n = writeFlashes([s], t, out);
      for (let i = 0; i < n; i++) {
        const w = out[i * 4 + 3];
        expect(Math.floor(w)).toBe(TONE_INDEX.alert);
        expect(w - Math.floor(w)).toBeLessThan(1);
        expect(Math.abs(out[i * 4] - 500)).toBeLessThanOrEqual(40);
        expect(Math.abs(out[i * 4 + 1] - 400)).toBeLessThanOrEqual(40);
        seen++;
      }
    }
    expect(seen).toBeGreaterThan(0);
  });

  test("takes only the newest strikes, and never overruns the buffer", () => {
    const list = Array.from({ length: 20 }, (_, i) => strike(i, 0));
    let most = 0;
    for (let t = 0; t <= STRIKE_LIFE_MS; t += 10)
      most = Math.max(most, writeFlashes(list, t, out));
    expect(most).toBeLessThanOrEqual(MAX_FLASHES);
    expect(most).toBeGreaterThan(FLASHES);
  });

  test("is deterministic for a strike id", () => {
    writeFlashes([strike(9, 0)], 1200, out);
    const first = out.slice(0, 4 * FLASHES);
    writeFlashes([strike(9, 0)], 1200, out);
    expect(out.slice(0, 4 * FLASHES)).toEqual(first);
  });
});

describe("flashPatches", () => {
  const w = 1600;
  const h = 1000;
  const box = planetBox(w, h);
  const cols = box.width / CELL;
  const rows = box.height / CELL;
  const flashes = new Float32Array(MAX_FLASHES * 4);
  const patches = new Int32Array(MAX_FLASHES * PATCH);
  const grouped = new Float32Array(MAX_FLASHES * 4);
  const random = seeded(7);
  const list = Array.from({ length: 8 }, (_, i) => ({
    id: i + 1,
    at: 0,
    tone: "warn" as const,
    ...strikePoint(w, h, random),
  }));
  const count = writeFlashes(list, 1000, flashes);
  const n = flashPatches(flashes, count, box, patches, grouped);
  const patch = (j: number) =>
    Array.from(patches.slice(j * PATCH, j * PATCH + PATCH));

  test("puts every visible flash in exactly one patch", () => {
    expect(count).toBeGreaterThan(0);
    const total = Array.from({ length: n }, (_, j) => patch(j)[5]).reduce(
      (a, b) => a + b,
      0,
    );
    expect(total).toBe(count);
    const seen = new Set<string>();
    for (let i = 0; i < total; i++)
      seen.add(Array.from(grouped.slice(i * 4, i * 4 + 4)).join());
    for (let i = 0; i < count; i++)
      expect(seen.has(Array.from(flashes.slice(i * 4, i * 4 + 4)).join())).toBe(
        true,
      );
  });

  test("keeps patches on the canvas and apart", () => {
    for (let j = 0; j < n; j++) {
      const [x, y, pw, ph] = patch(j);
      expect(x).toBeGreaterThanOrEqual(0);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(x + pw).toBeLessThanOrEqual(cols);
      expect(y + ph).toBeLessThanOrEqual(rows);
      for (let k = j + 1; k < n; k++) {
        const [ox, oy, ow, oh] = patch(k);
        const apart = x + pw < ox || ox + ow < x || y + ph < oy || oy + oh < y;
        expect(apart).toBe(true);
      }
    }
  });

  test("covers each flash's reach", () => {
    for (let j = 0; j < n; j++) {
      const [x, y, pw, ph, first, many] = patch(j);
      for (let i = first; i < first + many; i++) {
        const reach = Math.sqrt(grouped[i * 4 + 2] * FLASH_REACH);
        const cx = (grouped[i * 4] - box.x) / CELL;
        const cy = rows - (grouped[i * 4 + 1] - box.y) / CELL;
        expect(Math.max(0, cx - reach)).toBeGreaterThanOrEqual(x);
        expect(Math.min(cols, cx + reach)).toBeLessThanOrEqual(x + pw);
        expect(Math.max(0, cy - reach)).toBeGreaterThanOrEqual(y);
        expect(Math.min(rows, cy + reach)).toBeLessThanOrEqual(y + ph);
      }
    }
  });
});

describe("the shaders", () => {
  test("take their sizes and constants from dither.ts", () => {
    expect(FIELD_SHADER).toContain(`uniform vec4 uFlashes[${MAX_FLASHES}]`);
    expect(FIELD_SHADER).toContain(
      `uniform vec3 uPalette[${STRIKE_TONES.length}]`,
    );
    expect(FIELD_SHADER).toContain(LIGHT[0].toFixed(4));
    expect(FIELD_SHADER).toContain(`if (r > ${HALO.toFixed(4)})`);
  });

  test("leave the ground plain before fetching any cloud", () => {
    expect(FIELD_SHADER.indexOf(`if (r > ${HALO.toFixed(4)})`)).toBeLessThan(
      FIELD_SHADER.indexOf("textureLod"),
    );
  });

  test("wrap the baked noise on whole lattice periods", () => {
    for (const m of BAKE_SHADER.matchAll(
      /vec3\(([\d.]+), ([\d.]+), ([\d.]+)\)/g,
    ))
      for (const v of m.slice(1)) expect(Number(v) % 1).toBe(0);
  });

  test.each([
    ["bake", BAKE_SHADER],
    ["field", FIELD_SHADER],
    ["vertex", VERTEX_SHADER],
  ])("%s: interpolates nothing undefined", (_, src) => {
    expect(src.startsWith("#version 300 es\n")).toBe(true);
    expect(src).not.toMatch(/\$\{|undefined|NaN/);
  });
});

describe("mixRgb", () => {
  test("moves only the asked fraction of the way", () => {
    expect(mixRgb([0, 0, 0], [100, 200, 50], 0.16)).toEqual([16, 32, 8]);
    expect(mixRgb([23, 23, 23], [255, 0, 0], 0)).toEqual([23, 23, 23]);
  });
});
