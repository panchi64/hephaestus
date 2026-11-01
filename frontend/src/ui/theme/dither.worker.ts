import type { Strike } from "./dither";
import {
  createFieldLoop,
  type FieldLoop,
  type FieldState,
} from "./dither-loop";

/** Messages from the page. Strike times are absolute (`timeOrigin + now`), since a
 *  worker's `performance.now()` counts from its own start. */
export type ToWorker =
  | {
      kind: "start";
      canvas: OffscreenCanvas;
      state: FieldState;
      width: number;
      height: number;
    }
  | { kind: "state"; state: Partial<FieldState> }
  | { kind: "resize"; width: number; height: number }
  | { kind: "colors"; palette: Float32Array; ground: Float32Array };

/** `unsupported`: no WebGL2 on an OffscreenCanvas here; the page draws instead. */
export type FromWorker = { kind: "drawn" } | { kind: "unsupported" };

const local = (strikes: readonly Strike[]): Strike[] =>
  strikes.map((s) => ({ ...s, at: s.at - performance.timeOrigin }));

const toLocal = (state: Partial<FieldState>): Partial<FieldState> =>
  state.strikes ? { ...state, strikes: local(state.strikes) } : state;

const send = (message: FromWorker): void => postMessage(message);

let loop: FieldLoop | null = null;

addEventListener("message", (e: MessageEvent<ToWorker>) => {
  const m = e.data;
  if (m.kind === "start") {
    const state = { ...m.state, strikes: local(m.state.strikes) };
    loop = createFieldLoop(m.canvas, state, () => send({ kind: "drawn" }));
    if (!loop) return send({ kind: "unsupported" });
    loop.resize(m.width, m.height);
  } else if (m.kind === "state") loop?.update(toLocal(m.state));
  else if (m.kind === "resize") loop?.resize(m.width, m.height);
  else loop?.colors(m.palette, m.ground);
});
