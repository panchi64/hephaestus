/**
 * The thread's live browser: whether it is running, how to start and end it, the stream
 * of frames it paints, and the way input goes back.
 *
 * The browser is headless and lives in the platform VM, so this view is the only window
 * onto it. Each conversation owns one session (its tabs); sign-ins are one profile shared
 * across the app, which is not this module's business.
 */

import { API_BASE } from "~/lib/config";
import { api } from "~/lib/api";
import { getToken } from "~/lib/api/token";
import { handleAuthFailure } from "~/lib/api/client";
import { createInputQueue, type BrowserInput } from "./browserInput";

export interface BrowserSession {
  active: boolean;
  pageUrl: string | null;
}

interface BrowserSessionDTO {
  active: boolean;
  page_url: string | null;
}

const toSession = (dto: BrowserSessionDTO): BrowserSession => ({
  active: dto.active,
  pageUrl: dto.page_url,
});

const sessionPath = (conversationId: string): string =>
  `/browser/session/${encodeURIComponent(conversationId)}`;

/** Whether the thread has a live browser. Read-only: it never starts one. */
export async function fetchBrowserSession(
  conversationId: string,
): Promise<BrowserSession> {
  return toSession(
    await api.get<BrowserSessionDTO>(sessionPath(conversationId)),
  );
}

/** Start the thread's browser, or attach to the one already running. The backend
 *  answers 503 with a sentence when it could not open one; the caller surfaces that
 *  detail, since "why not" is the whole of what the operator can act on. */
export async function openBrowser(
  conversationId: string,
): Promise<BrowserSession> {
  return toSession(
    await api.post<BrowserSessionDTO>(sessionPath(conversationId), {}),
  );
}

/** End the session. The agent is told the operator closed it. */
export async function closeBrowser(conversationId: string): Promise<void> {
  await api.del(sessionPath(conversationId));
}

/** One frame of the page. `width` and `height` are the viewport in CSS pixels that the
 *  image depicts; the image's own pixel size may differ, so pointer positions scale by
 *  these. */
export interface BrowserFrame {
  type: "frame";
  data: string;
  width: number;
  height: number;
  url: string;
  title: string;
}

export type BrowserViewEvent = BrowserFrame | { type: "closed" };

/** Decode one SSE frame (the lines between blank lines), or null for a keepalive
 *  comment, an unknown event or anything malformed. */
export function parseBrowserEvent(frame: string): BrowserViewEvent | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const event = value as Record<string, unknown>;
  if (event.type === "closed") return { type: "closed" };
  if (
    event.type === "frame" &&
    typeof event.data === "string" &&
    typeof event.width === "number" &&
    typeof event.height === "number" &&
    event.width > 0 &&
    event.height > 0
  ) {
    return {
      type: "frame",
      data: event.data,
      width: event.width,
      height: event.height,
      url: typeof event.url === "string" ? event.url : "",
      title: typeof event.title === "string" ? event.title : "",
    };
  }
  return null;
}

/** How a view stream ended: the session closed, there is no session, or the transport
 *  dropped under a session that may still be alive. */
export type BrowserViewEnd = "closed" | "none" | "dropped";

/** A live stream is flushed at least a keepalive every ~15s; a read stalled past this is
 *  a dead connection. */
const READ_TIMEOUT_MS = 30_000;

/** Watch the thread's browser until it ends or `signal` aborts. Uses `fetch` and a body
 *  reader rather than `EventSource`, which cannot carry the bearer header. Frames are not
 *  replayed, so there is no resume position to remember. */
export async function streamBrowserView(
  conversationId: string,
  options: {
    onEvent: (event: BrowserViewEvent) => void;
    signal?: AbortSignal;
  },
): Promise<BrowserViewEnd> {
  const token = getToken();
  try {
    const res = await fetch(`${API_BASE}${sessionPath(conversationId)}/view`, {
      headers: {
        Accept: "text/event-stream",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      credentials: "omit",
      signal: options.signal,
    });
    if (res.status === 401 || res.status === 423) {
      handleAuthFailure();
      return "dropped";
    }
    if (res.status === 404) return "none";
    if (!res.ok || !res.body) return "dropped";

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
      ]).finally(() => clearTimeout(timer));
      if (result === "timeout") {
        await reader.cancel().catch(() => {});
        return "dropped";
      }
      if (result.done) return "dropped";
      buffer += decoder.decode(result.value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const event = parseBrowserEvent(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
        if (!event) continue;
        options.onEvent(event);
        if (event.type === "closed") return "closed";
      }
    }
  } catch {
    return "dropped";
  }
}

/** Post one input event. */
export async function sendBrowserInput(
  conversationId: string,
  event: BrowserInput,
): Promise<void> {
  await api.post(`${sessionPath(conversationId)}/input`, event);
}

/** An ordered, move-throttled channel of input to one thread's browser. */
export function createBrowserInput(
  conversationId: string,
  onError?: (error: unknown) => void,
) {
  return createInputQueue({
    send: (event) => sendBrowserInput(conversationId, event),
    onError,
  });
}
