import {
  Show,
  createEffect,
  createSignal,
  onCleanup,
  onMount,
  type Accessor,
  type JSX,
} from "solid-js";
import { useMediaQuery } from "~/lib/useMediaQuery";
import { accentOverrides } from "../theme/accent-overrides";
import {
  STRIKE_TONES,
  mixRgb,
  planetBox,
  type Rgb,
  type Strike,
  type StrikeTone,
} from "../theme/dither";
import { createFieldLoop, type FieldState } from "../theme/dither-loop";
import type { FromWorker, ToWorker } from "../theme/dither.worker";
import { useTheme } from "../theme/useTheme";

export interface DitherFieldProps {
  /** Live runs, with waiting-on-the-operator counted double. Turns the clouds faster. */
  load: Accessor<number>;
  /** Live sub-agents: one aurora curtain each. */
  aurora: Accessor<number>;
  strikes: Accessor<readonly Strike[]>;
}

const TONE_VAR: Record<Exclude<StrikeTone, "neutral">, string> = {
  info: "--accent-info",
  nominal: "--accent-nominal",
  warn: "--accent-warn",
  alert: "--accent-alert",
};

/** Any CSS colour as 0–255 RGB, resolved by a canvas so an operator-set accent in
 *  any syntax works. */
function toRgb(probe: CanvasRenderingContext2D, color: string): Rgb {
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = color;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
  return [r, g, b];
}

/** The tone palette at `--dither-tint` and the ground, as 0–1 RGB, from the tokens. */
function readPalette(): [Float32Array, Float32Array] | null {
  const probe = document.createElement("canvas").getContext("2d", {
    willReadFrequently: true,
  });
  if (!probe) return null;
  const cs = getComputedStyle(document.documentElement);
  const token = (name: string) => cs.getPropertyValue(name).trim();
  const on = toRgb(probe, token("--dither-on"));
  const tint = (Number.parseFloat(token("--dither-tint")) || 0) / 100;
  const palette = STRIKE_TONES.flatMap((tone) =>
    tone === "neutral"
      ? on
      : mixRgb(on, toRgb(probe, token(TONE_VAR[tone])), tint),
  );
  return [
    new Float32Array(palette.map((c) => c / 255)),
    new Float32Array(toRgb(probe, token("--bg")).map((c) => c / 255)),
  ];
}

/** Draws in a worker where the browser can hand it the canvas. */
const workerCapable = (): boolean =>
  typeof Worker === "function" &&
  "transferControlToOffscreen" in HTMLCanvasElement.prototype;

/** The field's commands, wherever it draws. */
interface Sink {
  state(next: Partial<FieldState>): void;
  resize(width: number, height: number): void;
  colors(palette: Float32Array, ground: Float32Array): void;
  stop(): void;
}

/**
 * The ambient field (§11.1): a gas giant in the bottom-right corner, ordered-dithered
 * on 2px cells. The canvas covers only the planet's box, one canvas pixel per cell,
 * scaled up hard. Opaque objects occlude it; dialogs sit over it.
 *
 * It draws in a worker on an OffscreenCanvas, so neither the compile nor a draw ever
 * lands on the page's thread; where a worker can't (no WebGL2 there), on the page.
 * Pacing lives in `createFieldLoop`. Without WebGL2 it draws nothing.
 *
 * Decorative, so `aria-hidden` and `pointer-events: none`.
 */
export function DitherField(props: DitherFieldProps): JSX.Element {
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const theme = useTheme();
  const [where, setWhere] = createSignal<"worker" | "page">(
    workerCapable() ? "worker" : "page",
  );
  let sink: Sink | null = null;

  const state = (): FieldState => ({
    load: props.load(),
    aurora: props.aurora(),
    strikes: props.strikes(),
    reduced: reduced(),
    hidden: document.hidden,
    focused: document.hasFocus(),
  });

  const place = (canvas: HTMLCanvasElement): void => {
    const box = planetBox(window.innerWidth, window.innerHeight);
    const s = canvas.style;
    s.left = `${box.x}px`;
    s.top = `${box.y}px`;
    s.width = `${box.width}px`;
    s.height = `${box.height}px`;
  };

  const inWorker = (canvas: HTMLCanvasElement): Sink => {
    const worker = new Worker(
      new URL("../theme/dither.worker.ts", import.meta.url),
      { type: "module" },
    );
    const post = (m: ToWorker, transfer: Transferable[] = []) =>
      worker.postMessage(m, transfer);
    // The worker's clock starts with it, so strike times travel absolute.
    const absolute = (strikes: readonly Strike[]) =>
      strikes.map((s) => ({ ...s, at: s.at + performance.timeOrigin }));
    worker.addEventListener("message", (e: MessageEvent<FromWorker>) => {
      if (e.data.kind === "drawn") canvas.style.visibility = "visible";
      else setWhere("page");
    });
    // A worker that fails to load or throws never says so; the page draws instead.
    worker.addEventListener("error", () => setWhere("page"));
    const offscreen = canvas.transferControlToOffscreen();
    const initial = state();
    post(
      {
        kind: "start",
        canvas: offscreen,
        state: { ...initial, strikes: absolute(initial.strikes) },
        width: window.innerWidth,
        height: window.innerHeight,
      },
      [offscreen],
    );
    return {
      state: (next) =>
        post({
          kind: "state",
          state: next.strikes
            ? { ...next, strikes: absolute(next.strikes) }
            : next,
        }),
      resize: (width, height) => post({ kind: "resize", width, height }),
      colors: (palette, ground) => post({ kind: "colors", palette, ground }),
      stop: () => worker.terminate(),
    };
  };

  const onPage = (canvas: HTMLCanvasElement): Sink | null => {
    const loop = createFieldLoop(canvas, state(), () => {
      canvas.style.visibility = "visible";
    });
    if (!loop) return null;
    loop.resize(window.innerWidth, window.innerHeight);
    return {
      state: (next) => loop.update(next),
      resize: (w, h) => loop.resize(w, h),
      colors: (p, g) => loop.colors(p, g),
      stop: () => loop.stop(),
    };
  };

  const paint = (): void => {
    const p = readPalette();
    if (p) sink?.colors(...p);
  };

  /** One canvas: a canvas handed to a worker can't be drawn on by the page, so the
   *  fallback gets a fresh one. */
  const Surface = (p: { mode: "worker" | "page" }): JSX.Element => {
    let canvas!: HTMLCanvasElement;
    const onResize = () => {
      place(canvas);
      sink?.resize(window.innerWidth, window.innerHeight);
    };
    const onVisibility = () => sink?.state({ hidden: document.hidden });
    // Focus moving into an iframe blurs the window first; `hasFocus` read once focus
    // settles counts the iframe.
    let settle: ReturnType<typeof setTimeout> | undefined;
    const onFocus = () => {
      clearTimeout(settle);
      settle = setTimeout(
        () => sink?.state({ focused: document.hasFocus() }),
        0,
      );
    };
    onMount(() => {
      sink = p.mode === "worker" ? inWorker(canvas) : onPage(canvas);
      place(canvas);
      paint();
      window.addEventListener("resize", onResize);
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("focus", onFocus);
      window.addEventListener("blur", onFocus);
    });
    onCleanup(() => {
      clearTimeout(settle);
      sink?.stop();
      sink = null;
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onFocus);
    });
    return (
      <canvas
        ref={canvas}
        class="heph-dither-field"
        style={{ visibility: "hidden" }}
        aria-hidden="true"
      />
    );
  };

  createEffect(() => {
    void theme.resolved;
    void accentOverrides();
    // `data-theme` and the override sheet land after this effect flushes; read the
    // tokens once they have.
    queueMicrotask(paint);
  });
  // Read before the `?.`: an optional call skips its arguments while there's no
  // sink yet, and an effect that read nothing never runs again.
  const forward = <K extends keyof FieldState>(
    key: K,
    read: () => FieldState[K],
  ) =>
    createEffect(() => {
      const value = read();
      sink?.state({ [key]: value });
    });
  forward("load", () => props.load());
  forward("aurora", () => props.aurora());
  forward("strikes", () => props.strikes());
  forward("reduced", reduced);

  return (
    <Show when={where()} keyed>
      {(mode) => <Surface mode={mode} />}
    </Show>
  );
}
