import { afterEach, beforeEach, describe, expect, test } from "bun:test";
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
