import { describe, expect, test } from "bun:test";
import { NO_FLASH_SCRIPT } from "./no-flash";
import {
  serializeOverrides,
  THEMES,
  type AccentOverrides,
} from "./accent-overrides";
import { ACCENT_TOKEN_NAMES, type AccentToken } from "./accents";

/**
 * The pre-paint script is hand-written ES5 that rebuilds, by eye, what
 * `serializeOverrides` builds from types. It cannot import that function — it
 * runs before the bundle exists — so the duplication is unavoidable and this is
 * what keeps it honest: run the real script against stubbed globals and compare
 * its sheet to the store's, character for character.
 *
 * **The comparison is only as good as what is fed to it**, which is why the main
 * fixture is generated from `ACCENT_TOKEN_NAMES` rather than written out.
 * Hand-written cases can only ever exercise the tokens that existed when someone
 * wrote them: add a sixth accent meaning, forget the script's own hard-coded copy
 * of that list, and every hand-written case still passes while the shipped accent
 * flashes on every cold load of the thing that was added. Generated, that is a
 * failure.
 *
 * It is also the only test the script has at all, which matters because it
 * splices `localStorage` into a stylesheet. The injection cases below are the
 * point, not decoration.
 *
 * Pure by construction: the stubs are plain objects, so this stays inside the
 * "no DOM in `bun test`" rule without a jsdom.
 */

/** Distinct, valid, and unrelated to any shipped value — a passing assertion must
 *  not be able to be a default coincidentally matching. */
function hexes(): () => string {
  let n = 0;
  return () =>
    `#${((n += 0x2a5f1b) % 0x1000000).toString(16).padStart(6, "0")}`;
}

/** An override on **every** token, in both themes — the case that catches a list the
 *  pre-paint script did not grow with the constants. */
function everyOverride(): AccentOverrides {
  const nextHex = hexes();
  const value: AccentOverrides = {};
  for (const theme of THEMES) {
    const tokens: Partial<Record<AccentToken, string>> = {};
    for (const token of ACCENT_TOKEN_NAMES) tokens[token] = nextHex();
    value[theme] = tokens;
  }
  return value;
}

interface Painted {
  theme: string | undefined;
  css: string | null;
}

/** Execute the script with a fake window, and report what it painted. */
function paint(options: {
  theme?: string | null;
  accents?: unknown;
  systemPrefersLight?: boolean;
}): Painted {
  const store: Record<string, string> = {};
  if (options.theme != null) store["odysseus:theme"] = options.theme;
  if (options.accents !== undefined)
    store["odysseus:accents"] = JSON.stringify(options.accents);

  const root = { dataset: {} as Record<string, string> };
  // A one-slot box rather than a bare `let`, so reading it back after the script
  // has run doesn't require convincing the type checker that a callback fired.
  const appended: { el?: { id?: string; textContent?: string } } = {};
  const document = {
    documentElement: root,
    createElement: () => ({}) as Record<string, unknown>,
    head: {
      appendChild: (el: { id?: string; textContent?: string }) => {
        appended.el = el;
      },
    },
  };
  const window = {
    matchMedia: () => ({ matches: options.systemPrefersLight === true }),
  };
  const localStorage = {
    getItem: (key: string) => store[key] ?? null,
  };

  new Function("window", "document", "localStorage", NO_FLASH_SCRIPT)(
    window,
    document,
    localStorage,
  );

  return { theme: root.dataset.theme, css: appended.el?.textContent ?? null };
}

describe("the theme half", () => {
  test("applies a stored preference", () => {
    expect(paint({ theme: "paper" }).theme).toBe("paper");
  });

  test("resolves 'system' against the OS", () => {
    expect(paint({ theme: "system", systemPrefersLight: true }).theme).toBe(
      "paper",
    );
    expect(paint({ theme: "system", systemPrefersLight: false }).theme).toBe(
      "phosphor",
    );
  });

  test("falls back to phosphor for junk or nothing stored", () => {
    expect(paint({}).theme).toBe("phosphor");
    expect(paint({ theme: "chartreuse" }).theme).toBe("phosphor");
  });
});

describe("the accent half agrees with serializeOverrides", () => {
  const cases: Record<string, AccentOverrides> = {
    "a single token": { phosphor: { accent: "#112233" } },
    "both themes, several tokens": {
      phosphor: { accent: "#112233", "accent-warn": "#445566" },
      paper: { "accent-alert": "#778899" },
    },
    // The one that has to keep passing as the constants grow — see the header.
    "every token this build has": everyOverride(),
  };

  for (const [name, overrides] of Object.entries(cases))
    test(name, () => {
      expect(paint({ accents: overrides }).css).toBe(
        serializeOverrides(overrides),
      );
    });

  test("and that fixture really does reach all of them", () => {
    // Guards the guard: a generated fixture that stopped generating anything would
    // make the comparison above pass by having nothing to disagree about.
    const css = serializeOverrides(everyOverride());
    for (const token of ACCENT_TOKEN_NAMES)
      expect(css).toContain(`--${token}:`);
    for (const theme of THEMES)
      expect(css).toContain(`html[data-theme="${theme}"]`);
  });

  test("writes no sheet at all when nothing is overridden", () => {
    // An empty `<style>` in every document is litter, and the store's own
    // `applyOverrides` declines to add one for the same reason.
    expect(paint({ accents: {} }).css).toBeNull();
    expect(paint({}).css).toBeNull();
  });
});

describe("the accent half refuses what the store refuses", () => {
  test("a value that would close the rule and open its own", () => {
    const css = paint({
      accents: {
        phosphor: {
          accent: "#000;}html{display:none",
          "accent-warn": "#f2a93b",
        },
      },
    }).css;
    expect(css).not.toContain("display:none");
    expect(css).toBe('html[data-theme="phosphor"]{--accent-warn:#f2a93b;}');
  });

  test("a key that is not an accent token", () => {
    // A `--bg` override would repaint the entire product from localStorage.
    const css = paint({ accents: { phosphor: { bg: "#ff0000" } } }).css;
    expect(css).toBeNull();
  });

  test("a session-signature blob left by an older build", () => {
    const css = paint({
      accents: { sessionAccent: { phosphor: { code: "#b98cff" } } },
    }).css;
    expect(css).toBeNull();
  });

  test("a stored blob that is not an object at all", () => {
    expect(paint({ accents: "nope" }).css).toBeNull();
    expect(paint({ accents: null }).css).toBeNull();
  });
});
