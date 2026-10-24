import {
  AURORA_MAX,
  CELL,
  advancePhase,
  cloudInterval,
  planetBox,
  planetDisc,
  type Disc,
  type Strike,
} from "./dither";
import { MAX_FLASHES, strikesAlive, writeFlashes } from "./dither-flashes";
import {
  createRenderer,
  type DitherRenderer,
  type FieldCanvas,
} from "./dither-gl";

/** Lightning flickers on tenths; the clouds redraw only when a cell would change. */
const STRIKE_FRAME_MS = 100;
/** How often to ask whether the shaders have finished compiling. */
const COMPILE_POLL_MS = 50;

/** What the platform tells the field. Strike times are `performance.now()` in the
 *  loop's own clock. */
export interface FieldState {
  load: number;
  aurora: number;
  strikes: readonly Strike[];
  reduced: boolean;
  hidden: boolean;
}

export interface FieldLoop {
  update(next: Partial<FieldState>): void;
  /** The viewport in CSS px. The caller places the canvas at `planetBox`. */
  resize(width: number, height: number): void;
  colors(palette: Float32Array, ground: Float32Array): void;
  stop(): void;
}

// Workers have no animation frames in every browser; a zero timeout still lands
// before the worker's canvas commits.
const nextFrame = (cb: (now: number) => void): number =>
  typeof requestAnimationFrame === "function"
    ? requestAnimationFrame(cb)
    : Number(setTimeout(() => cb(performance.now()), 0));
const cancelFrame = (id: number): void =>
  typeof cancelAnimationFrame === "function"
    ? cancelAnimationFrame(id)
    : clearTimeout(id);

/**
 * The field's pacing, in the page or in a worker: a draw happens only when it would
 * change a cell. A timer waits for the next one and a single frame draws it, so
 * nothing runs between draws or while hidden. Reduced motion and software rendering
 * hold a still frame with no lightning; software drops the aurora too.
 *
 * Null where WebGL2 can't run on `canvas`. `onDrawn` fires after the first draw.
 */
export function createFieldLoop(
  canvas: FieldCanvas,
  initial: FieldState,
  onDrawn: () => void,
): FieldLoop | null {
  let renderer: DitherRenderer | null = createRenderer(canvas);
  if (!renderer) return null;

  const state: FieldState = { ...initial };
  const flashes = new Float32Array(MAX_FLASHES * 4);
  let phase = 0;
  let last = performance.now();
  let viewport: [number, number] | null = null;
  let disc: Disc | null = null;
  let stale = true;
  let shown = false;
  let palette: [Float32Array, Float32Array] | null = null;

  let timer: ReturnType<typeof setTimeout> | undefined;
  let frame = 0;
  const halt = (): void => {
    clearTimeout(timer);
    timer = undefined;
    cancelFrame(frame);
    frame = 0;
  };
  const request = (): void => {
    if (!frame && renderer && !state.hidden) frame = nextFrame(paint);
  };
  const after = (ms: number): void => {
    clearTimeout(timer);
    timer = setTimeout(request, ms);
  };

  const place = (r: DitherRenderer): void => {
    stale = false;
    if (!viewport) return;
    const [w, h] = viewport;
    const box = planetBox(w, h);
    disc = planetDisc(w, h);
    const cols = box.width / CELL;
    const rows = box.height / CELL;
    // Resizing the buffer clears it, so only when the size actually changes.
    if (canvas.width !== cols) canvas.width = cols;
    if (canvas.height !== rows) canvas.height = rows;
    r.layout(box, disc);
  };

  function paint(now: number): void {
    frame = 0;
    const r = renderer;
    if (!r || !viewport) return;
    const ready = r.readiness();
    if (ready === "pending") return after(COMPILE_POLL_MS);
    if (ready === "failed") {
      renderer = null;
      return;
    }
    if (stale) place(r);
    const moving = !state.reduced && !r.still;
    if (moving) phase = advancePhase(phase, now - last, state.load);
    last = now;
    const flashing = moving && strikesAlive(state.strikes, now);
    // The first bake takes a strip per frame and shows nothing until it ends. Later
    // bakes wait out lightning, so no frame carries both.
    if (r.bake(moving ? phase : 0, moving && !flashing)) {
      frame = nextFrame(paint);
      return;
    }
    r.draw({
      phase: moving ? phase : 0,
      time: moving ? now / 1000 : 0,
      aurora: r.still ? 0 : Math.min(state.aurora, AURORA_MAX),
      flashes,
      flashCount: moving ? writeFlashes(state.strikes, now, flashes) : 0,
    });
    if (!shown) {
      shown = true;
      onDrawn();
    }
    if (!moving || !disc) return;
    after(flashing ? STRIKE_FRAME_MS : cloudInterval(disc.r, state.load));
  }

  const onLost = (e: Event): void => {
    e.preventDefault();
    halt();
    renderer = null;
  };
  const onRestored = (): void => {
    renderer = createRenderer(canvas);
    if (palette) renderer?.colors(...palette);
    stale = true;
    last = performance.now();
    request();
  };
  canvas.addEventListener("webglcontextlost", onLost);
  canvas.addEventListener("webglcontextrestored", onRestored);

  return {
    update(next) {
      const wasHidden = state.hidden;
      Object.assign(state, next);
      if (state.hidden) return halt();
      // Back from hidden, the clouds pick up where they were rather than jumping.
      if (wasHidden) last = performance.now();
      request();
    },
    resize(width, height) {
      viewport = [width, height];
      stale = true;
      request();
    },
    colors(p, g) {
      palette = [p, g];
      renderer?.colors(p, g);
      request();
    },
    stop() {
      halt();
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", onRestored);
    },
  };
}
