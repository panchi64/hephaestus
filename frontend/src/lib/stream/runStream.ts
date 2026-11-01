/**
 * Consume a run's Server-Sent Events stream.
 *
 * Uses `fetch` + a body reader rather than the native `EventSource`, because we
 * need a bearer header and a `Last-Event-ID` *request* header — neither of which
 * `EventSource` supports. Frames are parsed from the `text/event-stream` body;
 * the last seen `seq` drives reconnect-with-replay if the transport drops before
 * a terminal event. Resolves when the run ends, the caller aborts, or reconnects
 * are exhausted.
 *
 * A hidden page holds no connection, because the backend holds a run's notices while its
 * stream is open; the reader lets go on hide and reconnects from the last seq on show.
 */
import { API_BASE } from "~/lib/config";
import { getToken } from "~/lib/api/token";
import { handleAuthFailure } from "~/lib/api/client";
import { setBackendReachable } from "~/lib/stores/connectivity";
import { isTerminal, type RunEvent } from "./events";

export interface RunStreamOptions {
  onEvent: (event: RunEvent) => void;
  signal?: AbortSignal;
  /** Resume from after this seq (e.g. when re-opening a known run). */
  fromSeq?: number;
}

/** Thrown when the reconnect budget is exhausted without a terminal event — the
 *  transport gave up, but the backend run may still be alive. Distinct from a
 *  generic transport error so the caller can offer a re-attach affordance instead
 *  of silently treating the turn as ended. */
export class StreamDetachedError extends Error {
  constructor() {
    super("stream reconnect budget exhausted");
    this.name = "StreamDetachedError";
  }
}

// Exponential backoff: 500ms doubling to a 10s cap, giving up once the
// cumulative wait passes ~2 minutes — tolerates a backend restart mid-run
// instead of exhausting after a fixed, brief 3s window.
const RECONNECT_BASE_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 10_000;
const RECONNECT_MAX_TOTAL_DELAY_MS = 120_000;
// A live run flushes at least a keepalive comment every ~15s (see the backend SSE
// transport), so a read that stalls past this is a dead connection — typically a
// throttled background tab or a silently-dropped proxy. We cancel and reconnect
// with Last-Event-ID rather than hang forever (which would freeze the UI as
// "streaming"). Comfortably above the server's interval to avoid false trips.
const READ_TIMEOUT_MS = 30_000;

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const id = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(id);
      resolve();
    });
  });
}

const pageHidden = (): boolean =>
  typeof document !== "undefined" && document.visibilityState === "hidden";

/** Resolves once `pageHidden()` equals `hidden`, or the signal aborts; `stop` unlistens. */
function visibility(
  hidden: boolean,
  signal?: AbortSignal,
): { reached: Promise<void>; stop: () => void } {
  let stop = () => {};
  const reached = new Promise<void>((resolve) => {
    if (typeof document === "undefined") return;
    const check = () => {
      if (pageHidden() === hidden) done();
    };
    const done = () => {
      stop();
      resolve();
    };
    stop = () => {
      document.removeEventListener("visibilitychange", check);
      signal?.removeEventListener("abort", done);
    };
    document.addEventListener("visibilitychange", check);
    signal?.addEventListener("abort", done);
  });
  return { reached, stop };
}

/** Parse one SSE frame (the lines between blank lines) into an event. */
function parseFrame(frame: string): RunEvent | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  try {
    return JSON.parse(data) as RunEvent;
  } catch {
    return null;
  }
}

export async function streamRun(
  runId: string,
  { onEvent, signal, fromSeq }: RunStreamOptions,
): Promise<void> {
  let lastSeq: number | null = fromSeq ?? null;
  let failures = 0;
  let totalDelayMs = 0;

  while (!signal?.aborted) {
    if (pageHidden()) {
      await visibility(false, signal).reached;
      continue;
    }
    const hides = visibility(true, signal);
    // Its own controller so a connect that never answers can be abandoned; the caller's
    // abort still reaches it.
    const attempt = new AbortController();
    const forwardAbort = () => attempt.abort();
    signal?.addEventListener("abort", forwardAbort);
    let endedCleanly = false;
    try {
      const headers: Record<string, string> = { Accept: "text/event-stream" };
      const token = getToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
      if (lastSeq != null) headers["Last-Event-ID"] = String(lastSeq);

      const connectTimer = setTimeout(() => attempt.abort(), READ_TIMEOUT_MS);
      const res = await fetch(`${API_BASE}/runs/${runId}/events`, {
        headers,
        credentials: "omit",
        signal: attempt.signal,
      }).finally(() => clearTimeout(connectTimer));
      if (res.status === 401 || res.status === 423) {
        // Session expiry / vault re-lock, not a transport hiccup — retrying
        // would just burn the reconnect budget on a request that can never
        // succeed. Route through the same expiry path the REST client uses
        // (clears the token, flips the session store to locked) and stop.
        handleAuthFailure();
        return;
      }
      if (res.status === 404) return; // run is gone — nothing to stream
      if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
      setBackendReachable(true); // a live connection — the backend is reachable

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        const result = await Promise.race([
          reader.read(),
          new Promise<"timeout">((resolve) => {
            timer = setTimeout(() => resolve("timeout"), READ_TIMEOUT_MS);
          }),
          hides.reached.then(() => "hidden" as const),
        ]).finally(() => clearTimeout(timer));
        if (result === "timeout" || result === "hidden") {
          // A dead connection, or a page that hid: drop it and reconnect from lastSeq
          // (replaying the gap), which waits for the page to be visible again.
          await reader.cancel().catch(() => {});
          break;
        }
        const { value, done } = result;
        if (done) {
          endedCleanly = true;
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          // A chunk can already be read when the signal fires (a thread switch):
          // delivering its frames would fold the old run into whatever is bound now.
          if (signal?.aborted) return;
          const event = parseFrame(frame);
          if (!event) continue;
          // A real frame ends the outage: the budget bounds one, not the run's lifetime.
          failures = 0;
          totalDelayMs = 0;
          lastSeq = event.seq;
          onEvent(event);
          if (isTerminal(event)) return;
        }
      }
      // Body ended without a terminal event → the connection dropped; reconnect
      // from lastSeq to replay anything missed.
    } catch {
      if (signal?.aborted) return;
      const backoff = backoffFor(failures);
      failures += 1;
      totalDelayMs += backoff;
      if (totalDelayMs > RECONNECT_MAX_TOTAL_DELAY_MS) {
        setBackendReachable(false); // reconnects exhausted — treat the backend as down
        throw new StreamDetachedError();
      }
      await delay(backoff, signal);
    } finally {
      hides.stop();
      signal?.removeEventListener("abort", forwardAbort);
    }
    // A server that keeps accepting and closing would otherwise be reconnected in a
    // tight loop. Not charged to the budget: the backend answered.
    if (endedCleanly) {
      await delay(backoffFor(failures), signal);
      failures += 1;
    }
  }
}

const backoffFor = (failures: number): number =>
  Math.min(RECONNECT_BASE_DELAY_MS * 2 ** failures, RECONNECT_MAX_DELAY_MS);
