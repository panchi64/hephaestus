import { describe, expect, test } from "bun:test";
import {
  ACCENT_CONTRAST_FLOOR,
  MODE_BG,
  accentContrast,
  contrastRatio,
  meetsAccentFloor,
  normalizeHex,
  relativeLuminance,
} from "./contrast";
import { ACCENT_DEFAULTS, ACCENT_TOKENS } from "./accents";
import {
  CAUTION_TINT,
  DIAGRAM_CONTRAST_PAIRS,
  budgetedEmphases,
  emphasesInSource,
  legibleOn,
  mixHex,
} from "../components/mermaidTheme";

describe("normalizeHex", () => {
  test("expands 3-digit hex and lowercases", () => {
    expect(normalizeHex("#ABC")).toBe("#aabbcc");
    expect(normalizeHex("F0f")).toBe("#ff00ff");
  });

  test("accepts 6-digit hex with or without the hash, and trims", () => {
    expect(normalizeHex("  #34D67F ")).toBe("#34d67f");
    expect(normalizeHex("34d67f")).toBe("#34d67f");
  });

  test("rejects anything that is not a hex colour", () => {
    // The store concatenates the result into a stylesheet, so these must be
    // null rather than passed through — a `}` here would end the rule early.
    for (const junk of [
      "",
      "#12345",
      "#1234567",
      "red",
      "rgb(1,2,3)",
      "#ggg",
      "#000;}html{display:none",
    ])
      expect(normalizeHex(junk)).toBeNull();
  });
});

describe("relativeLuminance", () => {
  test("anchors at the sRGB extremes", () => {
    expect(relativeLuminance("#000000")).toBe(0);
    expect(relativeLuminance("#ffffff")).toBe(1);
  });

  test("weights green above red above blue", () => {
    // The 0.2126/0.7152/0.0722 coefficients are the whole point of the
    // function; a fixture of one channel each is what would catch them being
    // transposed.
    const r = relativeLuminance("#ff0000")!;
    const g = relativeLuminance("#00ff00")!;
    const b = relativeLuminance("#0000ff")!;
    expect(g).toBeGreaterThan(r);
    expect(r).toBeGreaterThan(b);
    expect(g).toBeCloseTo(0.7152, 4);
    expect(r).toBeCloseTo(0.2126, 4);
    expect(b).toBeCloseTo(0.0722, 4);
  });

  test("uses the linear ramp below the 0.03928 knee", () => {
    // #050505 is 5/255 ≈ 0.0196, under the knee, so it must divide by 12.92
    // rather than take the 2.4 power. The two branches differ by ~2x here.
    expect(relativeLuminance("#050505")).toBeCloseTo(5 / 255 / 12.92, 6);
  });

  test("is null for a non-colour", () => {
    expect(relativeLuminance("nope")).toBeNull();
  });
});

describe("contrastRatio", () => {
  test("black on white is 21:1", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
  });

  test("a colour against itself is 1:1", () => {
    expect(contrastRatio("#34d67f", "#34d67f")).toBeCloseTo(1, 5);
  });

  test("is order-independent", () => {
    // The lighter colour must always be the numerator; swapping the arguments
    // returning a reciprocal would be the bug this catches.
    const a = contrastRatio("#34d67f", "#000000")!;
    const b = contrastRatio("#000000", "#34d67f")!;
    expect(a).toBeCloseTo(b, 10);
    expect(a).toBeGreaterThan(1);
  });

  test("is null when either side is not a colour", () => {
    expect(contrastRatio("#000000", "nope")).toBeNull();
    expect(contrastRatio("nope", "#000000")).toBeNull();
  });
});

describe("accentContrast", () => {
  test("measures against the mode's own background", () => {
    // Pure white: 21:1 on Ink's black, 1:1 on Paper's white. Same input, and
    // the answers must not resemble each other — that is what proves the mode
    // is actually selecting a background rather than being ignored.
    expect(accentContrast("#ffffff", "phosphor")).toBeCloseTo(21, 1);
    expect(accentContrast("#ffffff", "paper")).toBeCloseTo(1, 1);
  });

  test("rounds to one decimal for display", () => {
    const ratio = accentContrast("#34d67f", "phosphor")!;
    expect(ratio).toBe(Math.round(ratio * 10) / 10);
  });
});

describe("meetsAccentFloor", () => {
  test("separates a passing colour from a failing one", () => {
    // #767676 on white is ~4.54:1; #808080 is ~3.95:1. Both sit clear of the
    // rounding band, so this asserts the floor itself rather than where the
    // rounding lands.
    expect(meetsAccentFloor("#767676", "paper")).toBe(true);
    expect(meetsAccentFloor("#808080", "paper")).toBe(false);
  });

  test("the verdict agrees with the figure the field displays", () => {
    // #777777 on white is ~4.477:1, which displays as `4.5:1`. It passes, and
    // it has to: showing "4.5:1 — below the 4.5:1 floor" reads as a broken
    // interface. Checking the unrounded ratio here would produce exactly that,
    // so this test pins the rounding decision, not an arithmetic detail.
    expect(accentContrast("#777777", "paper")).toBe(4.5);
    expect(meetsAccentFloor("#777777", "paper")).toBe(true);
  });

  test("is exactly at the boundary for a ratio that rounds to the floor", () => {
    // The comparison must be `>=`, not `>` — a colour displaying exactly the
    // floor is the floor being met, not missed.
    expect(ACCENT_CONTRAST_FLOOR).toBe(4.5);
    expect(meetsAccentFloor("#757575", "paper")).toBe(true);
  });

  test("treats a non-colour as passing", () => {
    // An empty or half-typed field is not a contrast problem, and warning about
    // legibility there would point at the wrong thing.
    expect(meetsAccentFloor("", "phosphor")).toBe(true);
  });

  test("every shipped accent clears the floor in its own mode", () => {
    // §12 says the tokens are tuned to pass and must be re-verified after any
    // hue change. This is that verification, run on every commit.
    for (const mode of ["phosphor", "paper"] as const)
      for (const { token } of ACCENT_TOKENS) {
        const hex = ACCENT_DEFAULTS[mode][token];
        const ratio = accentContrast(hex, mode)!;
        expect(
          ratio,
          `${token} (${hex}) on ${mode} ${MODE_BG[mode]} is ${ratio}:1`,
        ).toBeGreaterThanOrEqual(ACCENT_CONTRAST_FLOOR);
      }
  });
});

/* ==========================================================================
   The diagram register.

   A rendered diagram is the one surface whose colours are written into the
   markup as LITERALS rather than left to the cascade — it has to be, since the
   same markup is handed to an `<img>` in the lightbox, where no custom property
   reaches it. `mermaidTheme` reads those literals back out of `<html>`, so the
   values it emits are exactly the tokens below and nothing else. This is where
   the pairs it rests on are held to a floor.

   Two floors, not one, and the split is WCAG's: an edge is a graphical object
   (1.4.11, 3:1) and a label is text (1.4.3, 4.5:1). Reading tokens.css rather
   than restating its hexes, the way `accents.test.ts` does.
   ========================================================================== */

const TOKENS_CSS = await Bun.file(
  new URL("./tokens.css", import.meta.url).pathname,
).text();

/** tokens.css scopes Ink to `:root` and Paper to `[data-theme="paper"]`; slice
 *  at the Paper selector, anchored to the start of a line because the selector
 *  is also named in a comment near the top of the file. */
function tokenBlock(mode: "phosphor" | "paper"): string {
  const paperAt = TOKENS_CSS.search(/^\[data-theme="paper"\]\s*\{/m);
  expect(paperAt).toBeGreaterThan(-1);
  return mode === "phosphor"
    ? TOKENS_CSS.slice(0, paperAt)
    : TOKENS_CSS.slice(paperAt);
}

function token(mode: "phosphor" | "paper", name: string): string {
  // `--text` must not match `--text-dim:`, so the boundary is explicit.
  const match = tokenBlock(mode).match(
    new RegExp(`--${name}\\s*:\\s*(#[0-9a-fA-F]{3,8})\\s*;`),
  );
  expect(match, `tokens.css declares --${name} for ${mode}`).not.toBeNull();
  return normalizeHex(match![1])!;
}

describe("diagram tokens", () => {
  test("the stylesheet was actually read", () => {
    // Without this, a bad path would make every assertion below vacuous.
    expect(TOKENS_CSS.length).toBeGreaterThan(1000);
    expect(TOKENS_CSS).toContain("--surface-raised:");
  });

  for (const mode of ["phosphor", "paper"] as const)
    for (const pair of DIAGRAM_CONTRAST_PAIRS)
      test(`${mode} · ${pair.what}`, () => {
        const fg = token(mode, pair.fg);
        const bg = token(mode, pair.bg);
        const ratio = contrastRatio(fg, bg)!;
        expect(
          ratio,
          `${pair.fg} (${fg}) on ${pair.bg} (${bg}) is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(pair.floor);
      });

  test("the `line` token would NOT do for an edge, in either theme", () => {
    // The defect this register was reviewed for: an edge drawn at `line`
    // vanished into the frame around it and its arrowheads stopped being
    // legible at all. `line` is a hairline BORDER token and an edge is content,
    // so the two are not interchangeable — and the measurement is what says so
    // rather than the argument. If this ever passes 3:1, the reasoning in
    // `mermaidTheme`'s EDGE_WIDTH note needs rewriting, not the token swapping.
    for (const mode of ["phosphor", "paper"] as const) {
      const ratio = contrastRatio(token(mode, "line"), token(mode, "surface"))!;
      expect(
        ratio,
        `${mode}: line on surface is ${ratio.toFixed(2)}:1`,
      ).toBeLessThan(3);
    }
  });
});

describe("severity fills", () => {
  test("a caution tint keeps its label legible in both themes", () => {
    // Caution is a TINT and alert is a solid fill — fill weight, not hue, so the
    // two survive any colour vision. A tint still has to be a surface a label
    // can sit on.
    for (const mode of ["phosphor", "paper"] as const) {
      const tint = mixHex(
        token(mode, "accent-warn"),
        token(mode, "surface-raised"),
        CAUTION_TINT,
      );
      const ratio = contrastRatio(token(mode, "text-bright"), tint)!;
      expect(
        ratio,
        `${mode}: label on ${tint} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(ACCENT_CONTRAST_FLOOR);
    }
  });

  test("a caution tint is separable from an ordinary node", () => {
    // If the tint landed on the node fill, severity would be carried by the
    // reserved second line alone and the panel would stop being readable at a
    // glance — which is the whole reason the fill ladder exists.
    for (const mode of ["phosphor", "paper"] as const) {
      const node = token(mode, "surface-raised");
      const tint = mixHex(token(mode, "accent-warn"), node, CAUTION_TINT);
      expect(tint, `${mode}`).not.toBe(node);
      expect(
        Math.abs(relativeLuminance(tint)! - relativeLuminance(node)!),
      ).toBeGreaterThan(0.01);
    }
  });

  test("a solid fill's label is measured, not assumed", () => {
    // White on Ink's alert red is 3.0:1 and black on it is 6.9:1; on Paper the
    // verdict reverses. Assuming either one ships an illegible node in one of
    // the two themes, which is exactly what `legibleOn` exists to prevent.
    const chosen: Record<string, string> = {};
    for (const mode of ["phosphor", "paper"] as const) {
      const fill = token(mode, "accent-alert");
      const label = legibleOn(fill, [
        token(mode, "bg"),
        token(mode, "text-bright"),
      ]);
      chosen[mode] = label;
      const ratio = contrastRatio(label, fill)!;
      expect(
        ratio,
        `${mode}: ${label} on ${fill} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(ACCENT_CONTRAST_FLOOR);
    }
    expect(chosen.phosphor).not.toBe(chosen.paper);
  });

  test("a focused node's label is legible on the shipped signature", () => {
    for (const mode of ["phosphor", "paper"] as const) {
      const fill = ACCENT_DEFAULTS[mode].accent;
      const label = legibleOn(fill, [
        token(mode, "bg"),
        token(mode, "text-bright"),
      ]);
      const ratio = contrastRatio(label, fill)!;
      expect(
        ratio,
        `${mode}: ${label} on ${fill} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(ACCENT_CONTRAST_FLOOR);
    }
  });
});

describe("mixHex", () => {
  test("returns its endpoints", () => {
    expect(mixHex("#ffffff", "#000000", 1)).toBe("#ffffff");
    expect(mixHex("#ffffff", "#000000", 0)).toBe("#000000");
  });

  test("mixes in linear light, not in gamma-encoded channels", () => {
    // A naive channel average of black and white is #808080 (luminance 0.216);
    // the linear-light midpoint is perceptually mid-gray at ~#bcbcbc
    // (luminance 0.5). Mixing the naive way darkens every tint, and darkens it
    // by MORE on Ink than on Paper — which is how the two themes drift apart.
    const mid = mixHex("#ffffff", "#000000", 0.5);
    expect(relativeLuminance(mid)).toBeCloseTo(0.5, 2);
    expect(mid).not.toBe("#808080");
  });

  test("falls back to the surface when the tint is not a colour", () => {
    // The caller is always tinting a surface, so the surface is the safe answer.
    expect(mixHex("nope", "#161616", 0.5)).toBe("#161616");
  });
});

describe("the two-accent budget", () => {
  test("keeps at most two, ranked alert over caution over focus", () => {
    // A screen at rest is grayscale and carries at most two accents. A diagram
    // that asks for all three keeps the severities — they are what the operator
    // is scanning for — and the focused node falls back to a neutral emphasis,
    // which is still a fill and so still visible.
    expect([...budgetedEmphases(["alert", "caution", "focus"])]).toEqual([
      "alert",
      "caution",
    ]);
    expect([...budgetedEmphases(["focus", "caution"])]).toEqual([
      "caution",
      "focus",
    ]);
    expect([...budgetedEmphases(["focus"])]).toEqual(["focus"]);
    expect([...budgetedEmphases([])]).toEqual([]);
  });

  test("ignores classes that are not emphases", () => {
    expect([...budgetedEmphases(["decorative", "focus"])]).toEqual(["focus"]);
  });

  test("reads both of the syntaxes that attach a class", () => {
    // The budget has to be decided before the diagram renders, so both the
    // inline form and the standalone statement have to be found in the source.
    const source = [
      "flowchart TD",
      '  A["Build"]:::alert --> B["Ship"]',
      "  class B,C caution",
    ].join("\n");
    expect(emphasesInSource(source)).toEqual(new Set(["alert", "caution"]));
  });
});
