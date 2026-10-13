/**
 * Does the app actually boot?
 *
 * Every other test here is a pure function. This one spawns the real dev server
 * and loads the real page in a headless WebView, which is the only thing that
 * catches the class of break no unit test can see: a module that fails to
 * resolve, a Vite plugin that stops emitting, a worker whose dynamic import
 * can't be bundled, a top-level throw during mount. All of those leave #app
 * empty while every unit test still passes.
 *
 * It runs without the Python backend. With nothing to talk to, RequireAuth's
 * boot probe parks on its splash — which is fine, because what is being
 * asserted is that the shell mounted at all, not what it decided to show.
 *
 * Excluded from `bun run test` (it costs ~15s); `bun run test:smoke` runs it.
 */
import { afterAll, beforeAll, expect, test } from "bun:test";
import { join } from "node:path";

const PORT = 39_881;
const ORIGIN = `http://localhost:${PORT}`;
const FRONTEND_ROOT = join(import.meta.dir, "..", "..");

let dev: ReturnType<typeof Bun.spawn> | undefined;

/** Poll until `check` passes, so the test waits on the condition rather than on
 *  a fixed sleep that is either flaky or slow. */
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

test("the SPA boots, mounts, and renders its shell", async () => {
  const wv = new Bun.WebView({ headless: true });
  try {
    await wv.navigate(`${ORIGIN}/`);

    // Solid mounting into #app is the real assertion: a resolution failure or a
    // throw during mount leaves it empty, and everything below would still be
    // true of a blank page.
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

    expect(String(await wv.evaluate("document.title"))).toContain("Hephaestus");
    expect(String(await wv.evaluate("document.body.innerText"))).toContain(
      "Hephaestus",
    );

    // The no-flash theme script runs inline in the shell, ahead of the bundle.
    // If it stopped being emitted the app would still boot, just with a flash
    // of the wrong theme on every load — invisible to every other check.
    expect(
      String(
        await wv.evaluate(
          "document.documentElement.getAttribute('data-theme')",
        ),
      ),
    ).toMatch(/^(phosphor|paper)$/);
  } finally {
    wv.close();
  }
}, 120_000);
