import {
  createEffect,
  onCleanup,
  onMount,
  type Accessor,
  type JSX,
} from "solid-js";
import { useMediaQuery } from "~/lib/useMediaQuery";
import { accentOverrides } from "../theme/accent-overrides";
import {
  CELL,
  PULSE_TONES,
  mixRgb,
  shade,
  type Pulse,
  type PulseTone,
  type Rgb,
} from "../theme/dither";
import { useTheme } from "../theme/useTheme";

export interface DitherFieldProps {
  /** Live runs, with waiting-on-the-operator counted double. */
  load: Accessor<number>;
  pulses: Accessor<readonly Pulse[]>;
}

/** Stepped redraws, in the machine register: the field never eases between frames. */
const FRAME_MS = 100;

const TONE_VAR: Record<Exclude<PulseTone, "neutral">, string> = {
  info: "--accent-info",
  nominal: "--accent-nominal",
  warn: "--accent-warn",
  alert: "--accent-alert",
};

/** Any CSS colour as RGB, resolved by the canvas so an operator-set accent in any
 *  syntax works. */
function toRgb(probe: CanvasRenderingContext2D, color: string): Rgb {
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = color;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
  return [r, g, b];
}

/**
 * The ambient field behind every page (§11.1): ordered dither on the 4px grid, drawn
 * one canvas pixel per cell and scaled up hard. Opaque objects occlude it; dialogs sit
 * over it. It stops drawing while the tab is hidden, and under reduced motion holds a
 * still frame with no rings.
 *
 * Decorative, so `aria-hidden` and `pointer-events: none`.
 */
export function DitherField(props: DitherFieldProps): JSX.Element {
  let canvas!: HTMLCanvasElement;
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const theme = useTheme();

  let ctx: CanvasRenderingContext2D | null = null;
  let image: ImageData | undefined;
  let ground: Rgb = [0, 0, 0];
  let palette: Rgb[] = [];

  const draw = (): void => {
    if (!ctx || !image || palette.length === 0) return;
    const now = performance.now();
    const still = reduced();
    const t = still ? 0 : now / 1000;
    const pulses = still ? [] : props.pulses();
    const load = props.load();
    const { width, height, data } = image;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const tone = shade(x, y, t, now, load, pulses);
        const c = tone < 0 ? ground : palette[tone];
        const i = (y * width + x) * 4;
        data[i] = c[0];
        data[i + 1] = c[1];
        data[i + 2] = c[2];
        data[i + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  };

  const resize = (): void => {
    if (!ctx) return;
    // A window not yet laid out reports 0, and a zero-sized ImageData throws.
    canvas.width = Math.max(1, Math.ceil(window.innerWidth / CELL));
    canvas.height = Math.max(1, Math.ceil(window.innerHeight / CELL));
    image = ctx.createImageData(canvas.width, canvas.height);
    draw();
  };

  const readPalette = (): void => {
    const probe = document.createElement("canvas").getContext("2d", {
      willReadFrequently: true,
    });
    if (!probe) return;
    const cs = getComputedStyle(document.documentElement);
    const token = (name: string) => cs.getPropertyValue(name).trim();
    const on = toRgb(probe, token("--dither-on"));
    const tint = (Number.parseFloat(token("--dither-tint")) || 0) / 100;
    ground = toRgb(probe, token("--bg"));
    palette = PULSE_TONES.map((tone) =>
      tone === "neutral"
        ? on
        : mixRgb(on, toRgb(probe, token(TONE_VAR[tone])), tint),
    );
    draw();
  };

  let timer: ReturnType<typeof setInterval> | undefined;
  const stop = (): void => {
    clearInterval(timer);
    timer = undefined;
  };
  const sync = (): void => {
    const run = !document.hidden && !reduced();
    if (run && timer === undefined) timer = setInterval(draw, FRAME_MS);
    else if (!run) stop();
  };

  onMount(() => {
    ctx = canvas.getContext("2d");
    resize();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", sync);
    onCleanup(() => {
      stop();
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", sync);
    });
  });

  createEffect(() => {
    void theme.resolved;
    void accentOverrides();
    // `data-theme` and the override sheet land after this effect flushes; read the
    // tokens once they have.
    queueMicrotask(readPalette);
  });
  createEffect(() => {
    void reduced();
    sync();
  });
  // A still field still owes the operator a load step.
  createEffect(() => {
    void props.load();
    if (reduced()) draw();
  });

  return <canvas ref={canvas} class="heph-dither-field" aria-hidden="true" />;
}
