/**
 * Does `FramedOverlay`'s glass actually blur what is behind it?
 *
 * `backdrop-filter` only samples what is painted inside its **backdrop root**,
 * and `opacity < 1`, `filter`, `isolation: isolate` and a few other properties
 * each create one on any ancestor. So the effect can be switched off from a
 * parent the component does not own, and — this is the whole reason for this
 * file — **it fails silently**: the translucent fill still tints, so a dead
 * blur looks like a slightly lighter panel rather than like anything broken.
 * No unit test can see it, because it is a fact about how a browser composites
 * rather than about our markup.
 *
 * The concrete trap: `Modal` puts its fading backdrop *around* its dialog. Build
 * a framed overlay that way and the glass blurs nothing for as long as the fade
 * runs — which is exactly when the operator is looking at it. `FramedOverlay`
 * therefore makes the backdrop a **sibling** of the reveal, and the assertions
 * below pin both halves: that the real structure blurs, and that the structure
 * it was written to avoid does not.
 *
 * The second assertion is about geometry. The frame is drawn on the reveal's own
 * wrapper, so a wrapper pinned to the viewport puts the corner marks in the
 * corners of the *screen* — correct for the chat View's full-screen sheet, wrong
 * for a centered dialog. Centering therefore lives on a separate layer, and the
 * frame must measure the dialog.
 *
 * Excluded from `bun run test`; `bun run test:smoke` runs it.
 */
import { afterAll, beforeAll, expect, test } from "bun:test";
import { join } from "node:path";

const PORT = 39_883; // boot.smoke owns 39_881, accent-cascade.smoke owns 39_882
const ORIGIN = `http://localhost:${PORT}`;
const FRONTEND_ROOT = join(import.meta.dir, "..", "..", "..");

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

/** The DOM `FramedOverlay` builds, hand-assembled so the assertion is about the
 *  structure rather than about reaching a route behind the auth guard. Classes
 *  are copied from the component; if they drift, the geometry assertion fails. */
const BUILD_REAL = `(function(){
  var host = document.createElement('div');
  host.id = 'probe';
  host.innerHTML =
    '<div class="heph-fade-in fixed inset-0 z-50 bg-bg/70"></div>' +
    '<div class="pointer-events-none fixed inset-0 z-50 grid place-items-center p-6">' +
      '<div id="frame" class="heph-frame relative pointer-events-auto flex max-h-[85vh] min-h-0 w-full max-w-4xl flex-col" data-ready>' +
        '<div id="glass" class="heph-frame-surface heph-glass absolute inset-1.5"></div>' +
        '<div class="heph-frame-surface relative p-1.5" style="height:400px"></div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(host);
  return 'ok';
})()`;

/** The same overlay built the way `Modal` builds one: the fading backdrop as the
 *  dialog's ANCESTOR. This is the mistake, expressed as markup. */
const BUILD_NESTED = `(function(){
  var host = document.createElement('div');
  host.id = 'probe';
  host.innerHTML =
    '<div id="backdrop" class="fixed inset-0 z-50 grid place-items-center bg-bg/70" style="opacity:0.5">' +
      '<div id="frame" class="heph-frame relative w-full max-w-4xl" data-ready>' +
        '<div id="glass" class="heph-frame-surface heph-glass absolute inset-1.5"></div>' +
        '<div class="heph-frame-surface relative p-1.5" style="height:400px"></div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(host);
  return 'ok';
})()`;

const CLEANUP = `(function(){
  var p = document.getElementById('probe');
  if (p) p.remove();
  return 'ok';
})()`;

/** Walks up from the glass looking for anything that would make an ancestor a
 *  backdrop root. Returns the offending tag/class, or '' when the chain is
 *  clean — a string rather than a boolean so a failure names the culprit. */
const FIND_BACKDROP_ROOT = `(function(){
  var el = document.getElementById('glass').parentElement;
  while (el && el !== document.documentElement) {
    var cs = getComputedStyle(el);
    if (cs.isolation === 'isolate') return 'isolation on ' + el.className;
    if (parseFloat(cs.opacity) < 1) return 'opacity ' + cs.opacity + ' on ' + el.className;
    if (cs.filter && cs.filter !== 'none') return 'filter on ' + el.className;
    if (cs.mask && cs.mask !== 'none') return 'mask on ' + el.className;
    el = el.parentElement;
  }
  return '';
})()`;

test("the glass has a live backdrop-filter and no ancestor backdrop root", async () => {
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

    await wv.evaluate(BUILD_REAL);

    // The token resolved and the property survived the build. Lightning CSS has
    // dropped the standard property before, when the `-webkit-` prefix was
    // hand-written beside it — that shipped a panel opaque everywhere but Safari.
    const filter = String(
      await wv.evaluate(
        "getComputedStyle(document.getElementById('glass')).backdropFilter",
      ),
    );
    expect(filter).toContain("blur");

    // Nothing above the glass turns it into a no-op.
    expect(String(await wv.evaluate(FIND_BACKDROP_ROOT))).toBe("");

    await wv.evaluate(CLEANUP);

    // And the inverse, so a passing test above cannot be vacuous: built the way
    // `Modal` builds an overlay, the very same glass is inside a backdrop root.
    await wv.evaluate(BUILD_NESTED);
    expect(String(await wv.evaluate(FIND_BACKDROP_ROOT))).not.toBe("");
  } finally {
    wv.close();
  }
});

test("the frame measures the dialog, not the viewport", async () => {
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

    await wv.evaluate(BUILD_REAL);

    const box = JSON.parse(
      String(
        await wv.evaluate(`(function(){
          var r = document.getElementById('frame').getBoundingClientRect();
          return JSON.stringify({
            w: Math.round(r.width),
            h: Math.round(r.height),
            vw: window.innerWidth,
            vh: window.innerHeight,
          });
        })()`),
      ),
    ) as { w: number; h: number; vw: number; vh: number };

    // `max-w-4xl` is 56rem; the frame must be capped by it rather than spanning
    // the window. If centering ever moves back onto the reveal, this is the
    // assertion that catches it — the marks would be in the screen's corners.
    expect(box.w).toBeLessThan(box.vw);
    expect(box.h).toBeLessThan(box.vh);

    // Centered, within a pixel of rounding, in both axes.
    const centered = JSON.parse(
      String(
        await wv.evaluate(`(function(){
          var r = document.getElementById('frame').getBoundingClientRect();
          return JSON.stringify({
            dx: Math.abs(r.left - (window.innerWidth - r.width) / 2),
            dy: Math.abs(r.top - (window.innerHeight - r.height) / 2),
          });
        })()`),
      ),
    ) as { dx: number; dy: number };
    expect(centered.dx).toBeLessThanOrEqual(1);
    expect(centered.dy).toBeLessThanOrEqual(1);
  } finally {
    wv.close();
  }
});
