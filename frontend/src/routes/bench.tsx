import {
  For,
  Show,
  createSignal,
  onCleanup,
  onMount,
  type JSX,
} from "solid-js";
import { createStore, produce } from "solid-js/store";
import { MessageItem } from "~/features/chat/components/MessageItem";
import type { ChatMessage, TextBlock } from "~/features/chat/model";

/**
 * Transcript scroll bench — a dev-only harness, not a product surface.
 *
 * It exists because the thing being measured cannot be measured in the app: the
 * transcript's cost is a function of thread length, and a real long thread is the
 * operator's private data behind a lock this harness has no way through. So the
 * fixture is synthetic and the route is a SIBLING of `(app).tsx` — outside
 * `RequireAuth`, outside `AppShell` — which is what lets a profiler load it with no
 * backend, no session, and nothing of the operator's on screen. `models.ts` gates its
 * resources on `session.isAuthenticated`, so `MessageItem`'s model label resolves to
 * `""` without a single fetch.
 *
 * **The container is copied from `TranscriptView`/`ChatRoomScreen`, not imported.**
 * Importing `TranscriptView` would drag in the stream, the viewport and the thread
 * actions — the whole room — and a bench that needs a running backend measures
 * nothing. What matters for paint is the scroll container's classes and the reading
 * measure, and those are reproduced verbatim below; `MessageItem` itself is the real
 * component, which is where all the DOM being measured comes from.
 *
 * Two modes, because a settled fixture does not reproduce the whole problem:
 *   ?mode=settled  — history render. Every turn arrives complete.
 *   ?mode=streamed — a driver grows each answer a chunk per frame with
 *                    `streaming: true`, then settles it. This is the only way to
 *                    exercise the per-character reveal — the wrappers it puts in and,
 *                    once a block settles, takes back out.
 *
 * Flags toggle each candidate fix WITHOUT the fix being implemented, so a baseline and
 * every variant come out of one build:
 *   &n=40          turn pairs
 *   &cv=1          content-visibility on every turn but the last two
 *   &auto=1        run `compare()` as soon as the page is actually on screen
 *   &cps=300       characters per second the timed driver streams at
 *   &legibility=0  drop `text-rendering: optimizeLegibility`
 *
 * Driven from the console (or a devtools protocol client) via `window.__bench`.
 */

/** Same reading measure as the room — `ChatRoomScreen`'s `MEASURE`. */
const MEASURE = "mx-auto w-full max-w-4xl";

/** Turns at the tail that never get containment, whatever `&cv` says.
 *
 *  `transcriptScroll` measures everything from the BOTTOM — `scrollTop = scrollHeight`
 *  to follow, `scrollHeight - scrollTop - clientHeight` to decide attachment — so an
 *  estimated height is harmless above the viewport and corrupts both numbers the moment
 *  it applies to the live tail. */
const TAIL_KEEP = 2;

/** `content-visibility` is a LEVER HERE AND NOWHERE ELSE, and that is the finding rather
 *  than an omission. It shipped briefly and was taken back out: against the reveal leak
 *  it looked like the fix (120fps vs 112, p99 9.4ms vs 58ms), and against the leak's
 *  actual repair it does nothing at all — 150 turns scroll at p95 9.0ms / p99 9.3ms /
 *  zero dropped frames either way. What it bought was cover for a bug, at the price of
 *  paint containment that clips, a `scrollHeight` that is an estimate, and a blank frame
 *  after a large programmatic scroll. Kept as a flag because the measurement that ruled
 *  it out is worth being able to repeat on a longer thread. */
const DEFER_CSS =
  "[data-bench-defer]{content-visibility:auto;contain-intrinsic-size:auto 240px}";

const PROSE = `## Resolving the fence

The grammar walk does not trust the declaration — it *checks* it. Every path the
command names is resolved against the worktree root, and a path that escapes it is
refused before anything else is asked. That ordering is the whole point: a question
about a command that was never going to be allowed is a question that wastes the
operator's attention.

- **workspace** — writes stay under the worktree, no egress at all
- **network** — egress to the allowed-domain list, still no host writes
- **host** — anything, and therefore always reviewed

An undeclared egress is not reviewed but denied outright, since a worktree profile
allows no domains in the first place. The reviewer never sees it.

\`\`\`python
def reach_of(cmd: Command) -> Reach:
    if any(p.escapes(root) for p in cmd.paths):
        raise Refused(cmd)
    return cmd.declared
\`\`\`

| Level  | Mutating tools | Verdict source     |
| ------ | -------------- | ------------------ |
| Plan   | withheld       | —                  |
| Manual | parked         | operator           |
| Edit   | parked         | operator           |
| Auto   | ruled on       | judge, then review |

> An act the reviewer judges unrecoverable is asked about, never refused.

The fence holds the process to the declaration at every level, so what runs can never
exceed what was declared — \`seatbelt\` on macOS, \`bubblewrap\` on Linux. There is no
list of allowed programs anywhere, and that absence is deliberate: a list is a thing
that goes stale, and a structural check is not.
`;

const REASONING = `The operator is asking about the ordering, not the mechanism. Start
from why the path check runs first, then the three reach classes, then the fence. Keep
the table — it is the part they will come back to.`;

function makeTurn(i: number): [ChatMessage, ChatMessage] {
  const user: ChatMessage = {
    id: `u${i}`,
    role: "user",
    content: `Turn ${i}: walk me through how a command's declared reach is checked before it runs, and what happens at each permission level.`,
    createdAt: new Date(Date.now() - (200 - i) * 60_000).toISOString(),
  };
  const assistant: ChatMessage = {
    id: `a${i}`,
    role: "assistant",
    content: "",
    createdAt: new Date(Date.now() - (200 - i) * 60_000 + 20_000).toISOString(),
    model: "bench",
    blocks: [
      { kind: "thinking", id: `a${i}-t`, text: REASONING },
      {
        kind: "tool",
        id: `a${i}-c1`,
        tool: {
          id: `a${i}-c1`,
          name: "code.read",
          args: "path=backend/services/sandbox/fence.py",
          detail: "backend/services/sandbox/fence.py",
          status: "ok",
          outcome: "212 lines",
          result: "def build_profile(reach: Reach) -> str:\n    ...",
          elapsedMs: 140,
        },
      },
      {
        kind: "tool",
        id: `a${i}-c2`,
        tool: {
          id: `a${i}-c2`,
          name: "code.grep",
          args: "pattern=declared_reach",
          detail: "declared_reach",
          status: "ok",
          outcome: "9 hits",
          result:
            "services/tool_sensitivity.py:88\nservices/sandbox/fence.py:41",
          elapsedMs: 96,
        },
      },
      { kind: "text", id: `a${i}-x`, text: PROSE },
    ],
  };
  return [user, assistant];
}

function flag(
  params: URLSearchParams,
  name: string,
  fallback: boolean,
): boolean {
  const raw = params.get(name);
  return raw === null ? fallback : raw !== "0" && raw !== "false";
}

/**
 * One scroll run's result.
 *
 * **`dropped` is the headline, and getting its threshold right took two tries — both
 * failures are worth keeping written down, because each produced a confident number that
 * meant nothing.**
 *
 * A hard-coded 8.33ms bar reported 40% on a transcript that was in fact perfectly
 * smooth: at 120Hz the frame interval *is* 8.33ms, so half of a jitter distribution
 * centred on it lands above the line.
 *
 * Deriving the bar from the run's own median then reported 0% on a transcript that was
 * visibly stuttering, which is worse — a run where EVERY frame is slow has a slow
 * median, so nothing sits above it and a uniformly broken arm scores perfect.
 *
 * The bar has to come from the DISPLAY, not from the run. `measureRefresh` samples it
 * once from an idle page before any arm runs, and every arm is judged against that.
 * `fps` is the same fact stated so it cannot be misread: frames actually delivered per
 * second, against a refresh rate printed beside it.
 */
interface Sample {
  /** Share of frames that took longer than 1.5 display refreshes, as a percentage. */
  dropped: number;
  /** Frames actually delivered per second over the run. */
  fps: number;
  frames: number;
  p95: number;
  p99: number;
  /** `long-animation-frame` entries: main-thread work that blocked rendering. */
  longFrames: number;
  elements: number;
  revealSpans: number;
}

/** The display's refresh period in ms, sampled from an idle page.
 *
 *  Idle is the point — no scrolling, no DOM touched — so what comes back is the
 *  hardware's cadence rather than anything this page is doing to it. Taken as the median
 *  of the fastest half, which discards the odd hitch from whatever else the machine is
 *  running without flattering the result the way a plain minimum would. */
function measureRefresh(ms = 400): Promise<number> {
  return new Promise((resolve) => {
    const deltas: number[] = [];
    let last = performance.now();
    const start = last;
    const tick = (now: number): void => {
      deltas.push(now - last);
      last = now;
      if (now - start < ms) {
        requestAnimationFrame(tick);
        return;
      }
      const d = deltas.slice(1).sort((a, b) => a - b);
      resolve(d.length ? d[Math.floor(d.length * 0.25)] : 8.33);
    };
    requestAnimationFrame(tick);
  });
}

/** One frame's worth of an answer when the driver is stepped rather than timed —
 *  `pump()`, which has no clock to pace against. */
const CHUNK = 60;

/** Characters per second the timed driver plays an answer at, and the reason `&cps`
 *  exists at all.
 *
 *  The reveal paces its stagger against the OBSERVED gap between deltas, so the rate the
 *  fixture streams at is not cosmetic — it selects which regime the scheduler is in. A
 *  frame-per-chunk driver runs at roughly 3600 c/s, an order of magnitude faster than any
 *  real model, which pins `interval` at its floor and collapses the whole reveal into a
 *  few frames. Anything about how the fade *looks* has to be reproduced near a real rate
 *  or it is not being reproduced at all. */
const DEFAULT_CPS = 300;

export default function Bench(): JSX.Element {
  if (!import.meta.env.DEV) return <div>bench is dev-only</div>;

  const params = new URLSearchParams(
    typeof window === "undefined" ? "" : window.location.search,
  );
  const n = Math.max(1, Math.min(400, Number(params.get("n") ?? 40)));
  const streamed = (params.get("mode") ?? "settled") === "streamed";
  // A signal, not a constant: flipping containment on the SAME page load is how the
  // two numbers get compared without a reload in between, and a reload is the largest
  // source of variance in a measurement this small.
  const [cv, setCv] = createSignal(flag(params, "cv", false));
  const legibility = flag(params, "legibility", true);
  const cps = Math.max(
    10,
    Math.min(20_000, Number(params.get("cps") ?? DEFAULT_CPS)),
  );

  const seed = Array.from({ length: n }, (_, i) => makeTurn(i)).flat();
  // A store, like the real stream, so a delta patches one message in place rather than
  // recreating every row — measuring anything else would measure the wrong thing.
  const [messages, setMessages] = createStore<ChatMessage[]>(
    streamed
      ? seed.map((m) =>
          m.role === "assistant"
            ? {
                ...m,
                streaming: false,
                blocks: m.blocks!.map((b) =>
                  b.kind === "text" ? { ...b, text: "" } : b,
                ),
              }
            : m,
        )
      : seed,
  );

  const [ready, setReady] = createSignal(!streamed);
  // `&auto` progress, rendered in the corner. The measurement only runs while the page
  // is genuinely on screen, so the one person who can see it start is the one person who
  // needs to know whether it finished — a result that only exists on `window` is a
  // result they have to be told to go and read.
  const [status, setStatus] = createSignal<
    "idle" | "measuring" | "done" | "interrupted"
  >("idle");
  const [result, setResult] = createSignal<Record<string, Sample> | null>(null);
  const [refresh, setRefresh] = createSignal(8.33);
  let scrollEl: HTMLDivElement | undefined;

  // Stream one assistant turn at a time, a chunk at a time, exactly the shape the real
  // path takes: `streaming` on, text grows in place, `streaming` off. False once the
  // whole fixture has been played.
  let turn = 1; // index of the first assistant message
  let cursor = 0;
  const step = (chars = CHUNK): boolean => {
    if (turn >= messages.length) {
      setReady(true);
      return false;
    }
    cursor = Math.min(PROSE.length, cursor + chars);
    const at = turn;
    const done = cursor >= PROSE.length;
    setMessages(
      produce((list) => {
        const m = list[at];
        m.streaming = !done;
        const block = m.blocks?.find((b) => b.kind === "text") as
          TextBlock | undefined;
        if (block) block.text = PROSE.slice(0, cursor);
      }),
    );
    if (done) {
      turn += 2;
      cursor = 0;
    }
    return true;
  };

  onMount(() => {
    if (!legibility) document.documentElement.style.textRendering = "auto";
    onCleanup(() => {
      document.documentElement.style.textRendering = "";
    });

    if (!streamed) return;
    // Paced against the clock, not the frame, so the fixture streams at a rate a model
    // could plausibly produce — see `DEFAULT_CPS`. It is deliberately NOT the only way to
    // drive it: `requestAnimationFrame` does not fire in a hidden page, and
    // `__bench.pump()` plays the same steps on microtasks for a caller that has no
    // frames to wait on.
    let owed = 0;
    let last = performance.now();
    let raf = requestAnimationFrame(function frame(now) {
      owed += ((now - last) / 1000) * cps;
      last = now;
      let more = true;
      while (owed >= 1 && more) {
        const took = Math.min(CHUNK, Math.floor(owed));
        more = step(took);
        owed -= took;
      }
      raf = more ? requestAnimationFrame(frame) : 0;
    });
    onCleanup(() => cancelAnimationFrame(raf));
  });

  onMount(() => {
    const api = {
      ready,
      /** Play the fixture to the end without waiting on frames, yielding between chunks
       *  so Solid's effects and the reveal's own `queueMicrotask` pass both run. This is
       *  how a headless check drives the stream; `ready()` goes true at the end, and the
       *  reveal's terminal flush lands a few hundred ms after that. Resolves with the
       *  most wrappers that were in the DOM at any one moment.
       *
       *  Bounded rather than `while (true)`: a driver that stopped reporting done would
       *  otherwise hang its caller instead of failing it. */
      pump: async (): Promise<number> => {
        const cap = messages.length * Math.ceil(PROSE.length / CHUNK) + 8;
        // The peak is sampled here rather than read back afterwards. A caller polling
        // from outside the page races the terminal flush and can arrive to find zero
        // wrappers, which reads identically to a reveal that never ran — the one way an
        // assertion about unwrapping goes quietly vacuous.
        let peak = 0;
        for (let i = 0; i < cap; i++) {
          if (!step()) return peak;
          await new Promise<void>((r) => queueMicrotask(r));
          await new Promise<void>((r) => queueMicrotask(r));
          peak = Math.max(
            peak,
            document.querySelectorAll(".ody-token-in").length,
          );
        }
        throw new Error("bench: pump did not finish");
      },
      /** Mean cost of one full style-recalc + layout of the transcript, in ms.
       *
       *  The companion to `run`, and the one that works when the page is not visible:
       *  `requestAnimationFrame` does not fire in a hidden tab, so a profiler driving
       *  this headlessly gets nothing out of frame deltas. Forcing the relayout
       *  synchronously — perturb the container's width, then read a geometry property
       *  back — is measurable anywhere, scales with exactly what the fixes target (DOM
       *  size, per-character spans, contained subtrees), and is a real component of the
       *  frame budget rather than a proxy for it. It does NOT capture raster cost, so a
       *  visible-tab `run()` is still the headline number. */
      layout: (iterations = 30): { mean: number; p95: number } => {
        if (!scrollEl) throw new Error("bench: no scroll container");
        const el = scrollEl;
        const samples: number[] = [];
        for (let i = 0; i < iterations; i++) {
          el.style.paddingRight = i % 2 ? "1px" : "0px";
          const t = performance.now();
          void el.scrollHeight;
          samples.push(performance.now() - t);
        }
        el.style.paddingRight = "";
        samples.sort((a, b) => a - b);
        const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
        return {
          mean: Math.round(mean * 100) / 100,
          p95:
            Math.round(samples[Math.floor(samples.length * 0.95)] * 100) / 100,
        };
      },
      setCv,
      /** The A/B, run back to back on one page load: the transcript as it is, then the
       *  same transcript with off-screen turns deferred. Two passes each, first
       *  discarded — the first scroll through a long transcript pays one-off raster and
       *  decode costs that say nothing about steady-state scrolling. */
      /**
       * The whole A/B on one page load: the transcript as it was before either fix, then
       * each fix added, then both.
       *
       * One load rather than four, because a reload is the largest source of variance in
       * a measurement this small — and four arms rather than two because "it got faster"
       * is not an answer to "which change did that". The leak arm goes FIRST and is
       * undone by a reload at the end: `rewrap` only adds DOM, so running it before the
       * clean arms would contaminate them.
       */
      compare: async (ms = 2500) => {
        const refresh = await measureRefresh();
        setRefresh(refresh);
        const pass = async (on: boolean) => {
          setCv(on);
          await new Promise<void>((r) => requestAnimationFrame(() => r()));
          // A warm-up pass, discarded: the first scroll through a long transcript pays
          // one-off raster and image-decode costs that say nothing about steady state.
          if (!(await api.run(700, refresh))) return null;
          return api.run(ms, refresh);
        };
        api.rewrap();
        const leaked = await pass(false);
        const leakedDeferred = leaked ? await pass(true) : null;
        // Take the INJECTED spans back out — `[data-bench-span]`, not every
        // `.ody-token-in` — leaving the DOM the product now produces.
        document.querySelectorAll("[data-bench-span]").forEach((el) => {
          el.replaceWith(document.createTextNode(el.textContent ?? ""));
        });
        scrollEl?.normalize();
        const clean = leakedDeferred ? await pass(false) : null;
        const cleanDeferred = clean ? await pass(true) : null;
        setCv(flag(params, "cv", false));
        return leaked && leakedDeferred && clean && cleanDeferred
          ? { leaked, leakedDeferred, clean, cleanDeferred }
          : null;
      },
      /**
       * Put the leak back, so what the fix removed can be measured and not merely
       * asserted.
       *
       * The reveal used to leave one `.ody-token-in` span per character behind, for good
       * — around 280 per streamed answer, measured. Reproducing that here rather than
       * adding a flag to turn the fix off keeps the switch out of the product code, and
       * what is being measured is the DOM the leak produced, which is the thing that
       * cost anything. The spans carry a long-finished `animation-delay` for the same
       * reason the real ones did: a filled animation is retained state, not just an
       * element.
       */
      rewrap: (perAnswer = 280): number => {
        let made = 0;
        document.querySelectorAll(".ody-prose").forEach((prose) => {
          const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT);
          const texts: Text[] = [];
          for (let n = walker.nextNode(); n; n = walker.nextNode())
            texts.push(n as Text);
          let budget = perAnswer;
          // From the end backwards, which is where the real leak concentrated: a
          // character kept its wrapper if it was still resolving when its block stopped
          // being the trailing one.
          for (let i = texts.length - 1; i >= 0 && budget > 0; i--) {
            const node = texts[i];
            if (!node.parentNode || node.data.length === 0) continue;
            const take = Math.min(budget, node.data.length);
            const cut = node.data.length - take;
            const frag = document.createDocumentFragment();
            if (cut > 0)
              frag.appendChild(
                document.createTextNode(node.data.slice(0, cut)),
              );
            for (let c = cut; c < node.data.length; c++) {
              const span = document.createElement("span");
              span.className = "ody-token-in";
              // Tagged, so the cleanup below can remove exactly what was injected. A
              // blanket sweep of `.ody-token-in` would also eat the wrappers a stream
              // still in flight is depending on, cutting live fades to full opacity and
              // leaving the clean arms measuring a DOM the product never produces.
              span.dataset.benchSpan = "";
              span.style.setProperty("--reveal-ms", "320ms");
              span.style.animationDelay = "-9999ms";
              span.textContent = node.data[c];
              frag.appendChild(span);
            }
            node.parentNode.replaceChild(frag, node);
            budget -= take;
            made += take;
          }
        });
        return made;
      },
      counts: () => ({
        elements: document.getElementsByTagName("*").length,
        revealSpans: document.querySelectorAll(".ody-token-in").length,
      }),
      /** Scroll the transcript at a fixed velocity for `ms` and report how many frames
       *  it missed. rAF deltas rather than a paint timestamp because a dropped frame is
       *  exactly a rAF callback that arrived late. */
      run: (ms = 5000, refresh = 8.33, velocity = 40): Promise<Sample | null> =>
        new Promise((resolve) => {
          if (!scrollEl) throw new Error("bench: no scroll container");
          const el = scrollEl;
          el.scrollTop = 0;
          const deltas: number[] = [];
          let longFrames = 0;
          const obs =
            "PerformanceObserver" in window
              ? new PerformanceObserver((l) => {
                  longFrames += l.getEntries().length;
                })
              : null;
          try {
            obs?.observe({ type: "long-animation-frame", buffered: false });
          } catch {
            // Not every Chromium build exposes LoAF; the rAF deltas stand alone.
          }
          let last = performance.now();
          const start = last;
          const tick = (now: number): void => {
            // Frames stop coming the moment the page is hidden, and a run that resumed
            // afterwards would fold the whole gap into one enormous delta and report it
            // as a dropped frame. Abort instead, and let the caller start over.
            if (document.visibilityState !== "visible") {
              obs?.disconnect();
              resolve(null);
              return;
            }
            deltas.push(now - last);
            last = now;
            el.scrollTop += velocity;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 1)
              el.scrollTop = 0;
            if (now - start < ms) {
              requestAnimationFrame(tick);
              return;
            }
            obs?.disconnect();
            // Drop the first delta: it carries the gap since the last unrelated frame.
            const d = deltas.slice(1).sort((a, b) => a - b);
            const at = (q: number) =>
              Math.round(d[Math.floor(d.length * q)] * 100) / 100;
            resolve({
              frames: d.length,
              fps: Math.round((d.length / (now - start)) * 1000),
              dropped:
                Math.round(
                  (d.filter((x) => x > refresh * 1.5).length / d.length) * 1000,
                ) / 10,
              p95: at(0.95),
              p99: at(0.99),
              longFrames,
              elements: document.getElementsByTagName("*").length,
              revealSpans: document.querySelectorAll(".ody-token-in").length,
            });
          };
          requestAnimationFrame(tick);
        }),
    };
    (window as unknown as { __bench: typeof api }).__bench = api;

    // `&auto=1` arms the comparison to run the first time the page is genuinely on
    // screen. Frames do not tick in a hidden page, so a profiler driving this from
    // outside cannot start the scroll and then ask the operator to look — the page has
    // to notice it is being looked at and start itself.
    if (!params.get("auto")) return;
    let running = false;
    let settled = false;
    const stop = () => document.removeEventListener("visibilitychange", armed);
    const armed = () => {
      if (settled || running || document.visibilityState !== "visible") return;
      running = true;
      setStatus("measuring");
      void api.compare().then((result) => {
        running = false;
        // A null result means the page went away mid-run. Say so and stay armed rather
        // than reporting a number measured across a window nobody was looking at —
        // which is the whole reason this waits for visibility in the first place.
        setStatus(result ? "done" : "interrupted");
        if (!result) return;
        // And once there IS a result, stop listening. Staying armed would re-run the
        // whole comparison on the next tab switch and replace the numbers the operator
        // came back to read — reload to measure again.
        settled = true;
        stop();
        setResult(result);
        (window as unknown as { __benchResult: unknown }).__benchResult =
          result;
      });
    };
    document.addEventListener("visibilitychange", armed);
    onCleanup(stop);
    armed();
  });

  return (
    <div class="flex h-screen flex-col bg-bg text-text">
      {/* Injected rather than authored into theme.css: these are experiments, and an
          experiment that lives in the design system is a rule nobody meant to write. */}
      <style>{cv() ? DEFER_CSS : ""}</style>
      <Show when={status() !== "idle"}>
        <div class="pointer-events-none fixed bottom-3 right-3 z-50 border border-line bg-raised px-3 py-2 font-mono text-micro text-dim">
          <Show
            when={result()}
            fallback={
              <span>
                {status() === "measuring"
                  ? "measuring — keep this window in front"
                  : "interrupted — bring this window back in front"}
              </span>
            }
          >
            {(r) => (
              <div class="flex flex-col gap-0.5">
                <span class="text-bright">
                  display {Math.round((1000 / refresh()) * 10) / 10}Hz ·{" "}
                  {refresh()}ms
                </span>
                <For each={Object.entries(r())}>
                  {([name, v]) => (
                    <span>
                      {name}: {v.fps}fps · {v.dropped}% dropped · p95 {v.p95} ·
                      p99 {v.p99} · LoAF {v.longFrames} · {v.elements} el ·{" "}
                      {v.revealSpans} spans
                    </span>
                  )}
                </For>
              </div>
            )}
          </Show>
        </div>
      </Show>
      <div ref={scrollEl} class="min-h-0 flex-1 overflow-y-auto px-4 pt-2">
        <div class={MEASURE}>
          <For each={messages}>
            {(message, i) => (
              <div
                data-bench-defer={
                  cv() && i() < messages.length - TAIL_KEEP ? "" : undefined
                }
              >
                <MessageItem message={message} />
              </div>
            )}
          </For>
        </div>
      </div>
    </div>
  );
}
