/**
 * Does an accent override actually WIN the cascade?
 *
 * The whole accent-editing feature rests on one claim that no unit test can
 * check, because it is a fact about a browser rather than about our code:
 * `html[data-theme="…"]` is specificity (0,1,1) and therefore beats tokens.css's
 * `:root` and `[data-theme="paper"]` (0,1,0) **regardless of source order**.
 *
 * That is why the store can append its sheet whenever it likes and why the
 * pre-paint script can inject one before the bundle has even loaded. If it were
 * false, the feature would fail in the least visible way possible: the override
 * would apply in whichever order the sheets happened to land, so it would look
 * correct in development and silently stop working when a build reordered them.
 *
 * The sheet is therefore inserted as the FIRST child of `<head>` here — the
 * position source order would lose from — so the assertion can only pass on
 * specificity.
 *
 * Excluded from `bun run test`; `bun run test:smoke` runs it.
 */
import { afterAll, beforeAll, expect, test } from "bun:test";
import { join } from "node:path";
import { ACCENT_DEFAULTS } from "./accents";

const PORT = 39_882; // not 39_881 — boot.smoke.test.ts owns that one
const ORIGIN = `http://localhost:${PORT}`;
const FRONTEND_ROOT = join(import.meta.dir, "..", "..", "..");

/** Deliberately nothing like any shipped accent, so a passing assertion cannot
 *  be the default value coincidentally matching. */
const INK_OVERRIDE = "#ff00ff";
const PAPER_OVERRIDE = "#00ff00";

let dev: ReturnType<typeof Bun.spawn> | undefined;

async function waitFor(
  what: string,
  check: () => Promise<boolean>,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch (error) {
      lastError = error;
    }
    await Bun.sleep(250);
  }
  throw new Error(
    `timed out after ${timeoutMs}ms waiting for ${what}` +
      (lastError ? ` (last error: ${lastError})` : ""),
  );
}

beforeAll(async () => {
  dev = Bun.spawn(
    ["bun", "run", "dev", "--port", String(PORT), "--strictPort"],
    {
      cwd: FRONTEND_ROOT,
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env, BROWSER: "none" },
    },
  );
  await waitFor(
    "the dev server to serve",
    async () => (await fetch(ORIGIN)).ok,
    90_000,
  );
});

afterAll(() => {
  dev?.kill();
});

test("an accent override beats the token definitions", async () => {
  const wv = new Bun.WebView({ headless: true });
  try {
    await wv.navigate(`${ORIGIN}/`);
    await waitFor(
      "#app to mount",
      async () =>
        Number(
          await wv.evaluate(
            "document.getElementById('app')?.childElementCount ?? 0",
          ),
        ) > 0,
      30_000,
    );

    const readVar = async (name: string): Promise<string> =>
      String(
        await wv.evaluate(
          `getComputedStyle(document.documentElement).getPropertyValue('${name}').trim()`,
        ),
      );

    // Baseline: the shipped Ink accent, straight from tokens.css. If this ever
    // fails, the assertions below are measuring nothing.
    expect(await readVar("--accent")).toBe(ACCENT_DEFAULTS.phosphor.accent);

    // Insert the override sheet as the FIRST child of <head> — the position
    // source order would lose from, so only specificity can carry this.
    await wv.evaluate(`(function(){
      var s = document.createElement('style');
      s.id = 'heph-accent-overrides';
      s.textContent =
        'html[data-theme="phosphor"]{--accent:${INK_OVERRIDE};}' +
        'html[data-theme="paper"]{--accent:${PAPER_OVERRIDE};}';
      document.head.insertBefore(s, document.head.firstChild);
      return 'ok';
    })()`);

    expect(await readVar("--accent")).toBe(INK_OVERRIDE);

    // Flipping the theme must select the other rule on its own — this is what
    // buys `applyTheme` its ignorance of accents entirely.
    await wv.evaluate(
      "(function(){document.documentElement.dataset.theme='paper';return 'ok';})()",
    );
    expect(await readVar("--accent")).toBe(PAPER_OVERRIDE);
  } finally {
    wv.close();
  }
}, 120_000);
