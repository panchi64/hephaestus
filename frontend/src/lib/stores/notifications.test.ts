import { afterAll, beforeAll, describe, expect, spyOn, test } from "bun:test";
import type { Notification } from "~/lib/stream/notificationEvents";
import {
  markConversationRead,
  startNotifications,
  stopNotifications,
  useNotifications,
} from "./notifications";

function make(id: string, conversationId: string | null): Notification {
  return {
    id,
    kind: "run_completed",
    title: id,
    body: null,
    conversationId,
    runId: null,
    createdAt: new Date(2026, 9, 17, 12, Number(id.slice(1))).toISOString(),
    readAt: null,
    resolvedAt: null,
  };
}

const page = {
  items: [
    make("n5", "c1"),
    make("n4", "c2"),
    make("n3", "c1"),
    make("n2", "c1"),
    make("n1", null),
  ],
  unreadCount: 5,
};

describe("markConversationRead", () => {
  const fetchSpy = spyOn(globalThis, "fetch");
  const posted: string[] = [];

  beforeAll(async () => {
    fetchSpy.mockImplementation(
      Object.assign(
        (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.endsWith("/notifications/stream")) {
            return new Promise<Response>((_, reject) => {
              init?.signal?.addEventListener("abort", () =>
                reject(new DOMException("aborted", "AbortError")),
              );
            });
          }
          if (init?.method === "POST") {
            posted.push(url);
            return Promise.resolve(new Response(null, { status: 204 }));
          }
          return Promise.resolve(Response.json(page));
        },
        { preconnect: () => {} },
      ),
    );
    startNotifications();
    for (let i = 0; i < 200 && useNotifications().items.length === 0; i++)
      await Bun.sleep(5);
  });

  afterAll(() => {
    stopNotifications();
    fetchSpy.mockRestore();
  });

  test("a backfill is not an arrival", () => {
    expect(useNotifications().lastCreated).toBeNull();
  });

  test("marks the conversation's unread rows and skips `except`", () => {
    expect(useNotifications().items).toHaveLength(5);
    const marked = markConversationRead("c1", new Set(["n3"]));
    expect(marked.toSorted()).toEqual(["n2", "n5"]);
    const { items, unreadCount } = useNotifications();
    expect(items.find((n) => n.id === "n5")?.readAt).not.toBeNull();
    expect(items.find((n) => n.id === "n3")?.readAt).toBeNull();
    expect(items.find((n) => n.id === "n4")?.readAt).toBeNull();
    expect(unreadCount).toBe(3);
  });

  test("returns nothing once the rows are read, and for an unknown conversation", () => {
    expect(markConversationRead("c1", new Set(["n3"]))).toEqual([]);
    expect(markConversationRead("nope")).toEqual([]);
  });
});
