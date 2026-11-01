import { afterEach, beforeEach, describe, expect, jest, test } from "bun:test";
import type { RunEvent } from "./events";
import { streamRun } from "./runStream";

/** The slice of `document` the transport reads, with a visibility the test sets. */
class FakeDocument extends EventTarget {
  visibilityState: DocumentVisibilityState = "visible";

  show(state: DocumentVisibilityState): void {
    this.visibilityState = state;
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

interface Connection {
  lastEventId: string | null;
  cancelled: boolean;
  push: (frame: string) => void;
}

const encoder = new TextEncoder();
const frame = (seq: number, type: string) =>
  `data: ${JSON.stringify({ seq, type })}\n\n`;
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("a run stream on a page that hides", () => {
  const realFetch = globalThis.fetch;
  let page: FakeDocument;
  let connections: Connection[];

  beforeEach(() => {
    page = new FakeDocument();
    Object.defineProperty(globalThis, "document", {
      value: page,
      configurable: true,
    });
    connections = [];
    globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      let push: (frame: string) => void = () => {};
      const connection: Connection = {
        lastEventId: headers.get("Last-Event-ID"),
        cancelled: false,
        push: (f) => push(f),
      };
      connections.push(connection);
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          push = (f) => controller.enqueue(encoder.encode(f));
        },
        cancel() {
          connection.cancelled = true;
        },
      });
      return new Response(body, { status: 200 });
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
    Reflect.deleteProperty(globalThis, "document");
  });

  test("lets go while hidden and resumes from the last seq once shown", async () => {
    const seen: RunEvent[] = [];
    const done = streamRun("r1", { onEvent: (e) => seen.push(e) });
    await tick();
    connections[0].push(frame(1, "answer.delta"));
    await tick();

    page.show("hidden");
    await tick();
    expect(connections[0].cancelled).toBe(true);
    expect(connections).toHaveLength(1);

    page.show("visible");
    await tick();
    expect(connections).toHaveLength(2);
    expect(connections[1].lastEventId).toBe("1");

    connections[1].push(frame(2, "run.ended"));
    await done;
    expect(seen.map((e) => e.seq)).toEqual([1, 2]);
  });

  test("a page that starts hidden connects only once it is shown", async () => {
    page.visibilityState = "hidden";
    const done = streamRun("r1", { onEvent: () => {} });
    await tick();
    expect(connections).toHaveLength(0);

    page.show("visible");
    await tick();
    expect(connections).toHaveLength(1);
    connections[0].push(frame(1, "run.ended"));
    await done;
  });

  test("an abort while hidden ends the wait", async () => {
    page.visibilityState = "hidden";
    const abort = new AbortController();
    const done = streamRun("r1", { onEvent: () => {}, signal: abort.signal });
    abort.abort();
    await done;
    expect(connections).toHaveLength(0);
  });
});

describe("a run stream over a flaky transport", () => {
  const realFetch = globalThis.fetch;
  /** Settles every promise chain the transport has queued without moving the clock. */
  const drain = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };
  /** One frame, then the body errors: the connection dropped mid-stream. */
  const dropsAfter = (body: string) => {
    let sent = false;
    return new Response(
      new ReadableStream<Uint8Array>({
        // Errored on the next pull: an error raised alongside the enqueue discards it.
        pull(controller) {
          if (sent) controller.error(new Error("reset"));
          else controller.enqueue(encoder.encode(body));
          sent = true;
        },
      }),
      { status: 200 },
    );
  };

  beforeEach(() => jest.useFakeTimers());
  afterEach(() => {
    jest.useRealTimers();
    globalThis.fetch = realFetch;
  });

  test("a connect that never answers is abandoned and retried", async () => {
    let attempts = 0;
    globalThis.fetch = ((_url: unknown, init?: RequestInit) => {
      attempts += 1;
      if (attempts > 1)
        return Promise.resolve(dropsAfter(frame(1, "run.ended")));
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }) as typeof fetch;

    const done = streamRun("r1", { onEvent: () => {} });
    await drain();
    expect(attempts).toBe(1);
    jest.advanceTimersByTime(30_000);
    await drain();
    jest.advanceTimersByTime(500);
    await drain();
    expect(attempts).toBe(2);
    await done;
  });

  test("outages separated by real frames never add up to a detach", async () => {
    // Each outage alone waits ~25s; ten of them would pass the 120s budget if it
    // counted the run's whole lifetime instead of one outage.
    let call = 0;
    let seq = 0;
    globalThis.fetch = ((_url: unknown) => {
      call += 1;
      if (call > 60)
        return Promise.resolve(dropsAfter(frame(++seq, "run.ended")));
      if (call % 6 === 1)
        return Promise.resolve(dropsAfter(frame(++seq, "answer.delta")));
      return Promise.reject(new TypeError("network down"));
    }) as typeof fetch;

    const result: { outcome: "ended" | "detached" | null } = { outcome: null };
    const done = streamRun("r1", { onEvent: () => {} }).then(
      () => (result.outcome = "ended"),
      () => (result.outcome = "detached"),
    );
    for (let i = 0; i < 200 && result.outcome === null; i++) {
      jest.advanceTimersByTime(10_000);
      await drain();
    }
    await done;
    expect(result.outcome).toBe("ended");
  });
});
