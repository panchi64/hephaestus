import {
  BAKE_H,
  BAKE_ROWS,
  BAKE_STEP,
  BAKE_W,
  BAKE_WRAP,
  CELL,
  FIRST_BAKE_ROWS,
  STILL_BAKE_H,
  STILL_BAKE_W,
  type Box,
  type Disc,
} from "./dither";
import { MAX_FLASHES, PATCH, flashPatches } from "./dither-flashes";
import { BAKE_SHADER, FIELD_SHADER, VERTEX_SHADER } from "./dither-shader";

/** What one draw needs that changes between draws. */
export interface Frame {
  phase: number;
  /** Seconds, for the aurora's shimmer. */
  time: number;
  aurora: number;
  flashes: Float32Array;
  flashCount: number;
}

export type Readiness = "pending" | "ready" | "failed";

export interface DitherRenderer {
  /** Software rendering: the caller draws still frames only. */
  readonly still: boolean;
  /** Whether both programs have linked. Poll it; it never blocks on the compiler. */
  readiness(): Readiness;
  /** Bakes one strip if a bake is due and says whether the first is still unfinished.
   *  With `evolve` off, only the first bake advances. */
  bake(phase: number, evolve: boolean): boolean;
  layout(box: Box, disc: Disc): void;
  colors(palette: Float32Array, ground: Float32Array): void;
  draw(frame: Frame): void;
}

/** `low-power` keeps a dual-GPU machine on its integrated GPU. */
const OPTIONS: WebGLContextAttributes = {
  alpha: false,
  antialias: false,
  depth: false,
  stencil: false,
  preserveDrawingBuffer: false,
  powerPreference: "low-power",
};

const SOFTWARE = /swiftshader|llvmpipe|softpipe|software|basic render/i;
/** `KHR_parallel_shader_compile`'s query, which doesn't wait on the compiler. */
const COMPLETION_STATUS = 0x91b1;

/** The page's canvas, or the one a worker draws to. */
export type FieldCanvas = HTMLCanvasElement | OffscreenCanvas;

function webgl2(
  canvas: FieldCanvas,
  options: WebGLContextAttributes,
): WebGL2RenderingContext | null {
  // `HTMLCanvasElement` itself doesn't exist in a worker, so narrow by a member.
  return "transferControlToOffscreen" in canvas
    ? canvas.getContext("webgl2", options)
    : canvas.getContext("webgl2", options);
}

/** A context, and whether it renders in software: either the browser says it would
 *  be slow (`failIfMajorPerformanceCaveat`) or the renderer names a CPU rasteriser. */
function context(
  canvas: FieldCanvas,
): { gl: WebGL2RenderingContext; software: boolean } | null {
  const fast = webgl2(canvas, {
    ...OPTIONS,
    failIfMajorPerformanceCaveat: true,
  });
  if (fast) {
    const info = fast.getExtension("WEBGL_debug_renderer_info");
    const name = String(
      fast.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : fast.RENDERER),
    );
    return { gl: fast, software: SOFTWARE.test(name) };
  }
  const slow = webgl2(canvas, OPTIONS);
  return slow ? { gl: slow, software: true } : null;
}

/** Starts compiling and linking without asking for the result, which would block. */
function program(
  gl: WebGL2RenderingContext,
  fragment: string,
): WebGLProgram | null {
  const p = gl.createProgram();
  const vs = gl.createShader(gl.VERTEX_SHADER);
  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  if (!p || !vs || !fs) return null;
  gl.shaderSource(vs, VERTEX_SHADER);
  gl.shaderSource(fs, fragment);
  gl.compileShader(vs);
  gl.compileShader(fs);
  gl.attachShader(p, vs);
  gl.attachShader(p, fs);
  gl.bindAttribLocation(p, 0, "aPos");
  gl.linkProgram(p);
  return p;
}

function linkStatus(
  gl: WebGL2RenderingContext,
  p: WebGLProgram,
  parallel: boolean,
): Readiness {
  if (parallel && !gl.getProgramParameter(p, COMPLETION_STATUS))
    return "pending";
  if (gl.getProgramParameter(p, gl.LINK_STATUS)) return "ready";
  for (const s of gl.getAttachedShaders(p) ?? []) {
    const log = gl.getShaderInfoLog(s);
    if (log) console.error("DitherField:", log);
  }
  console.error("DitherField:", gl.getProgramInfoLog(p));
  return "failed";
}

function cloudTexture(
  gl: WebGL2RenderingContext,
  width: number,
  height: number,
): { tex: WebGLTexture; fb: WebGLFramebuffer } | null {
  const tex = gl.createTexture();
  const fb = gl.createFramebuffer();
  if (!tex || !fb) return null;
  const T = gl.TEXTURE_2D;
  gl.bindTexture(T, tex);
  const levels = Math.log2(Math.max(width, height)) + 1;
  gl.texStorage2D(T, levels, gl.R8, width, height);
  // Repeats around the planet; clamps at the poles.
  gl.texParameteri(T, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(T, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(T, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_NEAREST);
  gl.texParameteri(T, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, T, tex, 0);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  return { tex, fb };
}

/**
 * The field's WebGL2 renderer, or null where WebGL2 isn't available. The cloud deck
 * is baked into one of two textures in row strips, a strip per draw, and each finished
 * bake crossfades in over `BAKE_STEP` of phase; a draw itself is a cheap lookup.
 */
export function createRenderer(canvas: FieldCanvas): DitherRenderer | null {
  const made = context(canvas);
  if (!made) return null;
  const { gl, software } = made;
  const parallel = !!gl.getExtension("KHR_parallel_shader_compile");
  const bakeProgram = program(gl, BAKE_SHADER);
  const fieldProgram = program(gl, FIELD_SHADER);
  if (!bakeProgram || !fieldProgram) return null;

  const width = software ? STILL_BAKE_W : BAKE_W;
  const height = software ? STILL_BAKE_H : BAKE_H;
  const a = cloudTexture(gl, width, height);
  const b = cloudTexture(gl, width, height);
  if (!a || !b) return null;
  const clouds = [a, b];

  gl.bindVertexArray(gl.createVertexArray());
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 3, -1, -1, 3]),
    gl.STATIC_DRAW,
  );
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  const uniforms = () => {
    const u = (p: WebGLProgram, name: string) => gl.getUniformLocation(p, name);
    gl.useProgram(fieldProgram);
    gl.uniform1i(u(fieldProgram, "uCloudOld"), 0);
    gl.uniform1i(u(fieldProgram, "uCloudNew"), 1);
    gl.uniform2f(u(fieldProgram, "uBake"), width, height);
    return {
      bakeSize: u(bakeProgram, "uSize"),
      bakePhase: u(bakeProgram, "uPhase"),
      size: u(fieldProgram, "uSize"),
      origin: u(fieldProgram, "uOrigin"),
      disc: u(fieldProgram, "uDisc"),
      time: u(fieldProgram, "uTime"),
      phase: u(fieldProgram, "uPhase"),
      fade: u(fieldProgram, "uFade"),
      aurora: u(fieldProgram, "uAurora"),
      count: u(fieldProgram, "uFlashCount"),
      flashes: u(fieldProgram, "uFlashes"),
      palette: u(fieldProgram, "uPalette"),
      ground: u(fieldProgram, "uGround"),
    };
  };
  let at: ReturnType<typeof uniforms> | null = null;
  let failed = false;
  // Held until the programs link, then applied.
  let placed: { box: Box; disc: Disc } | null = null;
  let painted: { palette: Float32Array; ground: Float32Array } | null = null;

  const readiness = (): Readiness => {
    if (at) return "ready";
    if (failed) return "failed";
    for (const p of [bakeProgram, fieldProgram]) {
      const s = linkStatus(gl, p, parallel);
      if (s === "failed") failed = true;
      if (s !== "ready") return s;
    }
    at = uniforms();
    if (placed) layout(placed.box, placed.disc);
    if (painted) colors(painted.palette, painted.ground);
    return "ready";
  };

  // `front` fades in over `back` between `fadeFrom` and `fadeTo` (phase).
  let front = 0;
  let back = 0;
  let fadeFrom = 0;
  let fadeTo = 0;
  let first = true;
  const patches = new Int32Array(MAX_FLASHES * PATCH);
  const grouped = new Float32Array(MAX_FLASHES * 4);
  let target = 0;
  let row = 0;
  let bakePhase = 0;

  const bake = (phase: number, evolve: boolean): boolean => {
    if (!at) return true;
    if (!first && !evolve) return false;
    if (row < 0) {
      if (phase < fadeTo) return false;
      target = back === front ? 1 - front : back;
      bakePhase = phase + BAKE_STEP;
      row = 0;
    }
    if (first && row === 0) bakePhase = phase;
    const rows = Math.min(first ? FIRST_BAKE_ROWS : BAKE_ROWS, height - row);
    gl.useProgram(bakeProgram);
    gl.uniform2f(at.bakeSize, width, height);
    gl.uniform1f(at.bakePhase, bakePhase % BAKE_WRAP);
    gl.bindFramebuffer(gl.FRAMEBUFFER, clouds[target].fb);
    gl.viewport(0, 0, width, height);
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(0, row, width, rows);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.disable(gl.SCISSOR_TEST);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    row += rows;
    if (row < height) return first;

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, clouds[target].tex);
    gl.generateMipmap(gl.TEXTURE_2D);
    row = -1;
    if (first) {
      first = false;
      front = back = target;
      fadeFrom = fadeTo = phase;
    } else {
      back = front;
      front = target;
      fadeFrom = phase;
      fadeTo = phase + BAKE_STEP;
    }
    return false;
  };

  function layout(box: Box, disc: Disc): void {
    placed = { box, disc };
    if (!at) return;
    gl.useProgram(fieldProgram);
    gl.uniform2f(at.size, canvas.width, canvas.height);
    gl.uniform2f(at.origin, box.x / CELL, box.y / CELL);
    gl.uniform3f(at.disc, disc.cx, disc.cy, disc.r);
  }

  function colors(palette: Float32Array, ground: Float32Array): void {
    painted = { palette, ground };
    if (!at) return;
    gl.useProgram(fieldProgram);
    gl.uniform3fv(at.palette, palette);
    gl.uniform3fv(at.ground, ground);
  }

  const draw = (frame: Frame): void => {
    if (!at) return;
    const fade =
      fadeTo > fadeFrom
        ? Math.min(
            Math.max((frame.phase - fadeFrom) / (fadeTo - fadeFrom), 0),
            1,
          )
        : 1;
    gl.useProgram(fieldProgram);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform1f(at.time, frame.time);
    gl.uniform1f(at.phase, frame.phase);
    gl.uniform1f(at.fade, fade);
    gl.uniform1f(at.aurora, frame.aurora);
    gl.uniform1i(at.count, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, clouds[back].tex);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, clouds[front].tex);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (frame.flashCount === 0 || !placed) return;

    // Lightning covers a few small patches; only those are drawn again with it, each
    // with just the flashes that reach it.
    const n = flashPatches(
      frame.flashes,
      frame.flashCount,
      placed.box,
      patches,
      grouped,
    );
    gl.enable(gl.SCISSOR_TEST);
    for (let i = 0; i < n; i++) {
      const o = i * PATCH;
      gl.scissor(patches[o], patches[o + 1], patches[o + 2], patches[o + 3]);
      gl.uniform1i(at.count, patches[o + 5]);
      gl.uniform4fv(
        at.flashes,
        grouped,
        patches[o + 4] * 4,
        patches[o + 5] * 4,
      );
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    gl.disable(gl.SCISSOR_TEST);
  };

  return { still: software, readiness, bake, layout, colors, draw };
}
