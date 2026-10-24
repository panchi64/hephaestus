/**
 * Does a streamed answer leave anything behind?
 *
 * The reveal wraps arriving characters in spans so they resolve in on their own
 * schedule, and for a long time nothing ever took those wrappers out again: every
 * character that passed through the reveal window kept its span, its inline
 * `animation-delay` and its filled animation for the life of the session. A four-turn
 * fixture left over a thousand of them. That is invisible — the fade is over, the text
 * reads correctly — and it is paid on every repaint of a scrolling transcript, both in
 * the extra elements and in text that is shaped one character at a time.
 *
 * So the assertion is the one thing no unit test can see: after a stream ends, the DOM
 * holds no reveal wrappers and the answer still reads exactly as its source. The pure
 * scheduling rules behind it are covered in `streamReveal.test.ts`; this is the check
 * that the DOM half actually runs.
 *
 * It drives `/bench` (dev-only, no auth, no backend, synthetic content) through
 * `__bench.pump()` rather than through frames, because `requestAnimationFrame` does not
 * fire in a headless WebView.
 *
 * Excluded from `bun run test`; `bun run test:smoke` runs it.
 */
import { afterAll, beforeAll, expect, test } from "bun:test";
import { join } from "node:path";

const PORT = 39_882;
const ORIGIN = `http://localhost:${PORT}`;
const FRONTEND_ROOT = join(import.meta.dir, "..", "..", "..");

/**
 * The streamed answer's prose, as a selector — quoted for use inside the page-side
 * snippets below.
 *
 * This used to be `.heph-prose`, unqualified, which worked only for as long as the
 * assistant's answer was the single piece of rendered markdown on the page. The
 * operator's own turn renders as markdown too now, and it comes *first* in the
 * transcript — so the bare selector started answering with the question rather than
 * the reply, silently: the wrapper counts still looked sane, but `chars` tracked a
 * static user message, which pinned the "the prose got shorter" exemption below shut
 * and turned ordinary table churn into reported cuts.
 *
 * The distinguishing property is not position, so this does not ask for position. Only
 * the streaming answer renders block-by-block (`Markdown`'s `streamStable` path), so
 * only its prose has `[data-block-index]` children — which is also exactly the thing
 * the reveal operates on.
 */
const ANSWER_PROSE = "'.heph-prose:has([data-block-index])'";

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

test("a streamed answer leaves no reveal wrappers behind", async () => {
  const wv = new Bun.WebView({ headless: true });
  try {
    await wv.navigate(`${ORIGIN}/bench?mode=streamed&n=4`);
    await waitFor(
      "the bench to mount",
      async () => Boolean(await wv.evaluate("Boolean(window.__bench)")),
      30_000,
    );

    // `pump` resolves with the most wrappers that were in the DOM at once. Asserting on
    // it is what stops the check below from going vacuous: a reveal that stopped
    // wrapping anything at all would end with zero wrappers too, and look like a pass.
    // The peak is sampled inside the page because polling for it from out here races
    // the terminal flush.
    await wv.evaluate("window.__bench.pump().then((n) => (window.__peak = n))");
    // Waits on `__peak` itself, not on `ready()`. They are not the same signal: `step()`
    // flips `ready` and only then lets `pump` resolve, so a poll that sees `ready()` is
    // not yet proof the peak has been assigned — a race the IPC round-trip happens to
    // hide today and would stop hiding on a faster transport.
    await waitFor(
      "the fixture to finish streaming",
      async () => Boolean(await wv.evaluate("window.__peak !== undefined")),
      30_000,
    );
    expect(Number(await wv.evaluate("window.__peak"))).toBeGreaterThan(0);

    // The terminal flush is on a timer keyed to the last character's fade, so this
    // waits on the condition rather than guessing at the duration.
    await waitFor(
      "the reveal wrappers to be taken back out",
      async () =>
        Number(await wv.evaluate("window.__bench.counts().revealSpans")) === 0,
      10_000,
    );

    // Unwrapping rewrites text nodes, so the other half of the check is that it put
    // back exactly what it took: same characters, and re-joined into whole runs rather
    // than left one node per character (which is what costs the shaping).
    const text = String(
      await wv.evaluate(`document.querySelector(${ANSWER_PROSE}).textContent`),
    );
    expect(text).toContain("The fence holds the process to the declaration");

    // Measured on the LAST block, and that is load-bearing. Every earlier block is
    // re-rendered from source the moment a block after it begins — `Markdown`'s `<For>`
    // sees a changed raw string — so it ends up with whole text runs whether or not
    // anything re-joined them. The final block is the only one that keeps its wrappers
    // until the terminal flush, so it is the only one where the re-join is observable.
    const longestRun = Number(
      await wv.evaluate(
        "(() => {" +
          " const blocks = document.querySelectorAll('.heph-prose > [data-block-index]');" +
          " const last = blocks[blocks.length - 1];" +
          " const runs = [...last.querySelector('p').childNodes]" +
          "   .filter((n) => n.nodeType === 3).map((n) => n.data.length);" +
          " return Math.max(...runs);" +
          "})()",
      ),
    );
    expect(longestRun).toBeGreaterThan(20);
  } finally {
    wv.close();
  }
}, 180_000);

test("no character loses its wrapper while its fade is still running", async () => {
  const wv = new Bun.WebView({ headless: true });
  try {
    // A realistic rate, and that is the whole point of this test. The frame-per-chunk
    // driver runs ~3600 c/s, which pins the scheduler's `interval` at its floor and
    // collapses the reveal into a few frames — the regime below never happens, and the
    // bug this guards was invisible at that speed.
    await wv.navigate(`${ORIGIN}/bench?mode=streamed&n=1&cps=300`);
    await waitFor(
      "the bench to mount",
      async () => Boolean(await wv.evaluate("Boolean(window.__bench)")),
      30_000,
    );

    // Sample every frame from inside the page: how many wrappers exist, and how many of
    // them are still mid-fade. `document.getAnimations()` is the only thing that can
    // answer the second question — a wrapper that has been removed and one that has
    // finished look identical in the DOM.
    const raw = String(
      await wv.evaluate(`(async () => {
        const log = [];
        let zero = 0;
        await new Promise((res) => {
          const t0 = performance.now();
          const tick = () => {
            const spans = document.querySelectorAll('.heph-token-in').length;
            const prose = document.querySelector(${ANSWER_PROSE});
            const chars = prose ? prose.textContent.length : 0;
            let running = 0;
            for (const a of document.getAnimations()) {
              const el = a.effect && a.effect.target;
              if (!el || !el.classList || !el.classList.contains('heph-token-in')) continue;
              const d = a.effect.getTiming().duration || 320;
              const ct = typeof a.currentTime === 'number' ? a.currentTime : 0;
              if (a.playState !== 'finished' && ct / d < 1) running++;
            }
            log.push({ spans, running, chars });
            zero = spans === 0 ? zero + 1 : 0;
            if ((zero > 30 && log.length > 120) || performance.now() - t0 > 30000) { res(); return; }
            requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        });
        // A wrapper may only disappear once its fade is over, so a big drop in the
        // wrapper count while characters are still fading is the snap this guards. Two
        // qualifiers, both load-bearing:
        //
        // The tolerance separates things orders of magnitude apart — a couple of
        // characters finishing between frames and being written back as plain text is
        // ordinary churn (observed: 1-2), where the bug collapsed 74 wrappers to 1 in a
        // single frame with every one at opacity 0.
        //
        // A frame where the rendered prose got SHORTER is exempt, and that is a real
        // rule rather than a let-off. It means markdown finished a construct that took
        // characters out of the animatable space — most often a table, whose separator
        // row renders as nothing and whose cells are machine voice that animatableText
        // rejects outright. Those characters are supposed to stop fading and land hard
        // (section 8); Markdown has already rebuilt the block by then, so the wrappers
        // are gone before the reveal is even consulted.
        const cuts = [];
        for (let i = 1; i < log.length; i++)
          if (
            log[i - 1].running > 0 &&
            log[i - 1].spans - log[i].spans > 10 &&
            log[i].chars >= log[i - 1].chars
          )
            cuts.push({
              wasMidFade: log[i - 1].running,
              lost: log[i - 1].spans - log[i].spans,
            });
        return JSON.stringify({
          frames: log.length,
          peak: Math.max(...log.map((s) => s.spans)),
          framesMidFade: log.filter((s) => s.running > 0).length,
          cuts,
        });
      })()`),
    );
    const result = JSON.parse(raw) as {
      frames: number;
      peak: number;
      framesMidFade: number;
      cuts: { wasMidFade: number; lost: number }[];
    };

    // Without these the assertion below passes on a reveal that never animated at all.
    // Characters starting within a frame share a wrapper, so the window's couple of
    // hundred characters peak at a few dozen wrappers (observed ~46).
    expect(result.peak).toBeGreaterThan(20);
    expect(result.framesMidFade).toBeGreaterThan(30);
    // And an upper bound, because the failure in the other direction is silent: wrapping
    // a block that still holds wrappers puts a span inside each existing one and
    // multiplies the count every delta, which took it past a thousand.
    expect(result.peak).toBeLessThan(250);
    expect(result.cuts).toEqual([]);
  } finally {
    wv.close();
  }
}, 180_000);
