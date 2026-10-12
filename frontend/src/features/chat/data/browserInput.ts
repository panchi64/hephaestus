/**
 * What the operator's hands do to the live browser, and the order it arrives in.
 *
 * Input crosses a network hop and the page reacts to its sequence, so the queue is
 * serial: a press must land before its release, a key before the next key. The one
 * exception is the pointer moving, which is a stream of positions where only the latest
 * is true. Moves are therefore coalesced into a single pending slot and rate limited,
 * while every other event is sent exactly once and in order.
 */

export type MouseButton = "left" | "middle" | "right" | "none";

export type BrowserInput =
  | {
      type: "mouse";
      action: "down" | "up" | "move";
      x: number;
      y: number;
      button: MouseButton;
      click_count: number;
      modifiers: number;
    }
  | {
      type: "wheel";
      x: number;
      y: number;
      delta_x: number;
      delta_y: number;
      modifiers: number;
    }
  | {
      type: "key";
      action: "down" | "up";
      key: string;
      code: string;
      text: string;
      key_code: number;
      modifiers: number;
    }
  | { type: "text"; text: string }
  | { type: "navigate"; url: string }
  | { type: "history"; action: "back" | "forward" | "reload" };

/** Roughly thirty moves a second. */
export const MOVE_INTERVAL_MS = 33;

export interface InputQueueOptions {
  send: (event: BrowserInput) => Promise<void>;
  /** A send that failed. The queue carries on with what is behind it. */
  onError?: (error: unknown) => void;
  moveIntervalMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export interface InputQueue {
  push: (event: BrowserInput) => void;
  /** Drop what has not been sent. */
  clear: () => void;
}

const isMove = (event: BrowserInput): boolean =>
  event.type === "mouse" && event.action === "move";

export function createInputQueue(options: InputQueueOptions): InputQueue {
  const interval = options.moveIntervalMs ?? MOVE_INTERVAL_MS;
  const now = options.now ?? (() => performance.now());
  const sleep =
    options.sleep ??
    ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let pending: BrowserInput[] = [];
  let running = false;
  let lastMoveAt = Number.NEGATIVE_INFINITY;

  async function pump(): Promise<void> {
    if (running) return;
    running = true;
    while (pending.length > 0) {
      const event = pending[0];
      if (isMove(event)) {
        const wait = lastMoveAt + interval - now();
        if (wait > 0) await sleep(wait);
      }
      const next = pending.shift();
      if (next === undefined) continue;
      if (isMove(next)) lastMoveAt = now();
      try {
        await options.send(next);
      } catch (error) {
        options.onError?.(error);
      }
    }
    running = false;
  }

  return {
    push(event) {
      const tail = pending.length - 1;
      if (isMove(event) && tail >= 0 && isMove(pending[tail])) {
        pending[tail] = event;
      } else {
        pending.push(event);
      }
      void pump();
    },
    clear() {
      pending = [];
    },
  };
}
