import { describe, expect, test } from "bun:test";
import { serializeOverrides } from "./accent-overrides";

/**
 * `serializeOverrides` is the one pure function in the accent store, and it is
 * the one that matters most: its output is concatenated into a `<style>` element,
 * from values that came out of `localStorage`. Everything here is about what it
 * must refuse to emit.
 *
 * The rest of the store touches `document` and `localStorage`, so it is not
 * unit-tested — per `frontend/CLAUDE.md`, only pure logic lives in `bun test`.
 */

describe("serializeOverrides", () => {
  test("emits one scoped rule per mode that has overrides", () => {
    const css = serializeOverrides({
      phosphor: { accent: "#112233" },
      paper: { "accent-warn": "#445566" },
    });
    expect(css).toBe(
      'html[data-theme="phosphor"]{--accent:#112233;}' +
        'html[data-theme="paper"]{--accent-warn:#445566;}',
    );
  });

  test("the selector is specific enough to beat the token definitions", () => {
    // tokens.css declares these at `:root` (0,1,0) and `[data-theme="paper"]`
    // (0,1,0). `html[data-theme=…]` is (0,1,1), so it wins on specificity
    // rather than on source order — which is what makes the override immune to
    // where the sheet lands in the head.
    const css = serializeOverrides({ paper: { accent: "#000000" } });
    expect(css.startsWith('html[data-theme="paper"]{')).toBe(true);
  });

  test("emits nothing for an empty or absent override set", () => {
    expect(serializeOverrides({})).toBe("");
    expect(serializeOverrides({ phosphor: {} })).toBe("");
  });

  test("drops values that are not hex colours", () => {
    // The injection case: a value ending the declaration and opening its own
    // rule. `normalizeHex` rejects it, so nothing reaches the sheet.
    const css = serializeOverrides({
      phosphor: {
        accent: "#000;}html{display:none}",
        "accent-warn": "#f2a93b",
      } as never,
    });
    expect(css).not.toContain("display:none");
    expect(css).toBe('html[data-theme="phosphor"]{--accent-warn:#f2a93b;}');
  });

  test("drops keys that are not accent tokens", () => {
    // Iterating the known token list rather than the object's own keys is what
    // makes this true — a `--bg` override would repaint the whole product.
    const css = serializeOverrides({
      phosphor: { bg: "#ff0000", accent: "#112233" } as never,
    });
    expect(css).not.toContain("--bg");
    expect(css).toBe('html[data-theme="phosphor"]{--accent:#112233;}');
  });

  test("normalizes shorthand and case on the way out", () => {
    const css = serializeOverrides({ phosphor: { accent: "#ABC" } });
    expect(css).toBe('html[data-theme="phosphor"]{--accent:#aabbcc;}');
  });

  test("ignores a session-signature blob left by an older build", () => {
    const css = serializeOverrides({
      sessionAccent: { phosphor: { code: "#112233" } },
    } as never);
    expect(css).toBe("");
  });

  test("emits tokens in registry order, not insertion order", () => {
    // Stable output means the sheet's text only changes when a value does.
    const css = serializeOverrides({
      phosphor: { "accent-info": "#111111", accent: "#222222" },
    });
    expect(css).toBe(
      'html[data-theme="phosphor"]{--accent:#222222;--accent-info:#111111;}',
    );
  });
});
