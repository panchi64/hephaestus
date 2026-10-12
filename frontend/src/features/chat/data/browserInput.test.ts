import { describe, expect, test } from "bun:test";
import { createInputQueue, type BrowserInput } from "./browserInput";

const move = (x: number): BrowserInput => ({
  type: "mouse",
  action: "move",
  x,
  y: 0,
  button: "none",
  click_count: 0,
  modifiers: 0,
});
const down = (x: number): BrowserInput => ({
  type: "mouse",
  action: "down",
  x,
  y: 0,
  button: "left",
  click_count: 1,
  modifiers: 0,
});
const key = (k: string): BrowserInput => ({
  type: "key",
  action: "down",
  key: k,
  code: k,
  text: k,
  key_code: 0,
  modifiers: 0,
});

const label = (e: BrowserInput): string =>
  e.type === "mouse"
    ? `${e.action}${e.x}`
    : e.type === "key"
      ? `key:${e.key}`
      : e.type;

/** A queue whose sends are held until released, on a clock the test moves. */
function harness(interval = 33) {
  const sent: string[] = [];
  const gates: (() => void)[] = [];
  let clock = 0;
  const sleeps: number[] = [];
  const queue = createInputQueue({
    moveIntervalMs: interval,
    now: () => clock,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock += ms;
    },
    send: (event) => {
      sent.push(label(event));
      return new Promise<void>((resolve) => gates.push(resolve));
    },
  });
  const release = async () => {
    gates.shift()?.();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  };
  return { queue, sent, release, sleeps, tick: (ms: number) => (clock += ms) };
}

describe("input queue", () => {
  test("sends discrete events in order, never dropping one", async () => {
    const h = harness();
    h.queue.push(down(1));
    h.queue.push(key("a"));
    h.queue.push({ type: "text", text: "hi" });
    expect(h.sent).toEqual(["down1"]);
    await h.release();
    expect(h.sent).toEqual(["down1", "key:a"]);
    await h.release();
    await h.release();
    expect(h.sent).toEqual(["down1", "key:a", "text"]);
  });

  test("coalesces moves queued behind an in-flight request", async () => {
    const h = harness();
    h.queue.push(move(1));
    h.queue.push(move(2));
    h.queue.push(move(3));
    h.queue.push(move(4));
    expect(h.sent).toEqual(["move1"]);
    await h.release();
    h.tick(100);
    await h.release();
    await h.release();
    expect(h.sent).toEqual(["move1", "move4"]);
  });

  test("a press is never merged into the moves around it", async () => {
    const h = harness();
    h.queue.push(key("a"));
    h.queue.push(move(1));
    h.queue.push(move(2));
    h.queue.push(down(3));
    h.queue.push(move(4));
    h.queue.push(move(5));
    for (let i = 0; i < 6; i++) await h.release();
    expect(h.sent).toEqual(["key:a", "move2", "down3", "move5"]);
  });

  test("rate limits moves to the interval", async () => {
    const h = harness(40);
    h.queue.push(move(1));
    await h.release();
    h.queue.push(move(2));
    // No time has passed since the last move, so it waits the full interval.
    await h.release();
    expect(h.sleeps).toEqual([40]);
    expect(h.sent).toEqual(["move1", "move2"]);
  });

  test("a failed send does not stop what is behind it", async () => {
    const errors: unknown[] = [];
    const sent: string[] = [];
    const queue = createInputQueue({
      send: async (event) => {
        sent.push(label(event));
        if (sent.length === 1) throw new Error("boom");
      },
      onError: (e) => errors.push(e),
    });
    queue.push(key("a"));
    queue.push(key("b"));
    await new Promise((r) => setTimeout(r, 0));
    expect(sent).toEqual(["key:a", "key:b"]);
    expect(errors.length).toBe(1);
  });

  test("clear drops what has not been sent", async () => {
    const h = harness();
    h.queue.push(key("a"));
    h.queue.push(key("b"));
    h.queue.clear();
    await h.release();
    expect(h.sent).toEqual(["key:a"]);
  });
});
