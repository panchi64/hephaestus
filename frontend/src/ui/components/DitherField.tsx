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
  AURORA_MAX,
  CELL,
  MAX_STRIKES,
  STRIKE_TONES,
  advancePhase,
  liveStrikes,
  mixRgb,
  planetDisc,
  type Rgb,
  type Strike,
  type StrikeTone,
} from "../theme/dither";
import { FRAGMENT_SHADER, VERTEX_SHADER } from "../theme/dither-shader";
import { useTheme } from "../theme/useTheme";

export interface DitherFieldProps {
  /** Live runs, with waiting-on-the-operator counted double. Turns the clouds faster. */
  load: Accessor<number>;
  /** Live sub-agents: one aurora curtain each. */
  aurora: Accessor<number>;
  strikes: Accessor<readonly Strike[]>;
}

/** Stepped redraws, in the machine register: the field never eases between frames.
 *  The clouds are slow enough for a quarter second; lightning flickers on tenths. */
const CLOUD_FRAME_MS = 250;
const STRIKE_FRAME_MS = 100;

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

interface Gl {
  gl: WebGLRenderingContext;
  size: WebGLUniformLocation | null;
  disc: WebGLUniformLocation | null;
  time: WebGLUniformLocation | null;
  phase: WebGLUniformLocation | null;
  aurora: WebGLUniformLocation | null;
  strikes: WebGLUniformLocation | null;
  palette: WebGLUniformLocation | null;
  ground: WebGLUniformLocation | null;
}

function compile(
  gl: WebGLRenderingContext,
  type: number,
  source: string,
): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
  console.error("DitherField:", gl.getShaderInfoLog(shader));
  return null;
}

/** The program and its one full-screen triangle, or null where WebGL can't run it. */
function setup(canvas: HTMLCanvasElement): Gl | null {
  const gl = canvas.getContext("webgl", {
    alpha: false,
    antialias: false,
    depth: false,
    stencil: false,
    preserveDrawingBuffer: false,
  });
  if (!gl) return null;
  const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
  const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
  const program = gl.createProgram();
  if (!vs || !fs || !program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return null;
  gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 3, -1, -1, 3]),
    gl.STATIC_DRAW,
  );
  const pos = gl.getAttribLocation(program, "aPos");
  gl.enableVertexAttribArray(pos);
  gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);
  const at = (name: string) => gl.getUniformLocation(program, name);
  return {
    gl,
    size: at("uSize"),
    disc: at("uDisc"),
    time: at("uTime"),
    phase: at("uPhase"),
    aurora: at("uAurora"),
    strikes: at("uStrikes"),
    palette: at("uPalette"),
    ground: at("uGround"),
  };
}

/**
 * The ambient field behind every page (§11.1): a gas giant in the bottom-right corner,
 * ordered-dithered on 2px cells, shaded on the GPU one canvas pixel per cell and scaled
 * up hard. Opaque objects occlude it; dialogs sit over it. It stops drawing while the
 * tab is hidden, holds a still frame with no lightning under reduced motion, and draws
 * nothing where WebGL is unavailable.
 *
 * Decorative, so `aria-hidden` and `pointer-events: none`.
 */
export function DitherField(props: DitherFieldProps): JSX.Element {
  let canvas!: HTMLCanvasElement;
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const theme = useTheme();

  let gl: Gl | null = null;
  const slots = new Float32Array(MAX_STRIKES * 4);
  let phase = 0;
  let last = performance.now();

  /** Draws one frame and says whether a strike is still flashing. */
  const draw = (): boolean => {
    if (!gl) return false;
    const now = performance.now();
    const still = reduced();
    if (!still) phase = advancePhase(phase, now - last, props.load());
    last = now;
    const live = still ? [] : liveStrikes(props.strikes(), now);
    slots.fill(-1);
    live.forEach((s, i) => {
      slots.set([s.x, s.y, now - s.at, STRIKE_TONES.indexOf(s.tone)], i * 4);
    });
    const disc = planetDisc(window.innerWidth, window.innerHeight);
    const g = gl.gl;
    g.uniform2f(gl.size, canvas.width, canvas.height);
    g.uniform3f(gl.disc, disc.cx, disc.cy, disc.r);
    g.uniform1f(gl.time, still ? 0 : now / 1000);
    g.uniform1f(gl.phase, phase);
    g.uniform1f(gl.aurora, Math.min(props.aurora(), AURORA_MAX));
    g.uniform4fv(gl.strikes, slots);
    g.drawArrays(g.TRIANGLES, 0, 3);
    return live.length > 0;
  };

  const resize = (): void => {
    // A window not yet laid out reports 0, and a zero-sized buffer can't be drawn.
    canvas.width = Math.max(1, Math.ceil(window.innerWidth / CELL));
    canvas.height = Math.max(1, Math.ceil(window.innerHeight / CELL));
    gl?.gl.viewport(0, 0, canvas.width, canvas.height);
    draw();
  };

  const readPalette = (): void => {
    if (!gl) return;
    const probe = document.createElement("canvas").getContext("2d", {
      willReadFrequently: true,
    });
    if (!probe) return;
    const cs = getComputedStyle(document.documentElement);
    const token = (name: string) => cs.getPropertyValue(name).trim();
    const on = toRgb(probe, token("--dither-on"));
    const tint = (Number.parseFloat(token("--dither-tint")) || 0) / 100;
    const palette = STRIKE_TONES.flatMap((tone) =>
      tone === "neutral"
        ? on
        : mixRgb(on, toRgb(probe, token(TONE_VAR[tone])), tint),
    );
    gl.gl.uniform3fv(
      gl.palette,
      palette.map((c) => c / 255),
    );
    gl.gl.uniform3fv(
      gl.ground,
      toRgb(probe, token("--bg")).map((c) => c / 255),
    );
    draw();
  };

  let timer: ReturnType<typeof setTimeout> | undefined;
  const stop = (): void => {
    clearTimeout(timer);
    timer = undefined;
  };
  const running = (): boolean => !!gl && !document.hidden && !reduced();
  const tick = (): void => {
    const flashing = draw();
    timer = running()
      ? setTimeout(tick, flashing ? STRIKE_FRAME_MS : CLOUD_FRAME_MS)
      : undefined;
  };
  const sync = (): void => {
    if (!running()) stop();
    else if (timer === undefined) tick();
  };

  const init = (): void => {
    gl = setup(canvas);
    resize();
    readPalette();
    sync();
  };
  const onLost = (e: Event): void => {
    e.preventDefault();
    stop();
    gl = null;
  };

  onMount(() => {
    init();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", sync);
    canvas.addEventListener("webglcontextlost", onLost);
    canvas.addEventListener("webglcontextrestored", init);
    onCleanup(() => {
      stop();
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", sync);
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", init);
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
  // A new strike flashes now, not at the end of a quarter-second cloud frame.
  createEffect(() => {
    void props.strikes();
    if (timer === undefined) return;
    stop();
    tick();
  });
  // A still field still owes the operator its aurora.
  createEffect(() => {
    void props.aurora();
    if (reduced()) draw();
  });

  return <canvas ref={canvas} class="heph-dither-field" aria-hidden="true" />;
}
