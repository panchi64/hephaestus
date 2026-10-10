import { beforeEach, describe, expect, test } from "bun:test";

/* `localStorage` does not exist under `bun test`, so a map-backed stand-in is installed
   before the module is used. The helpers read storage on every call, so a plain import
   after the stub is enough. */
const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (key: string) => store.get(key) ?? null,
  setItem: (key: string, value: string) => void store.set(key, value),
  removeItem: (key: string) => void store.delete(key),
};

const { loadDraft, moveDraft, saveDraft } = await import("./composerDraft");

beforeEach(() => store.clear());

describe("composer drafts", () => {
  test("round-trips a draft, and an empty save removes it", () => {
    saveDraft("chat:a", "hello");
    expect(loadDraft("chat:a")).toBe("hello");
    saveDraft("chat:a", "");
    expect(store.has("ody.draft.chat:a")).toBe(false);
    expect(loadDraft(undefined)).toBe("");
  });
});

/* The case this exists for: a new thread is typed into under `chat:new`, the stream
   binds its real id, and the room re-keys to `chat:<id>`. Without the move the draft
   the operator is still writing vanishes on the key change. */
describe("moveDraft", () => {
  test("moves the draft and clears the source", () => {
    saveDraft("chat:new", "half a thought");
    moveDraft("chat:new", "chat:42");
    expect(loadDraft("chat:42")).toBe("half a thought");
    expect(loadDraft("chat:new")).toBe("");
  });

  test("is a no-op when the source is empty or whitespace", () => {
    saveDraft("chat:new", "   ");
    moveDraft("chat:new", "chat:42");
    expect(loadDraft("chat:42")).toBe("");
    // Left alone rather than cleared: nothing was carried, so nothing is owed.
    expect(loadDraft("chat:new")).toBe("   ");
  });

  test("never overwrites a draft already under the destination", () => {
    // Both are the operator's; replacing one with the other loses one.
    saveDraft("chat:new", "incoming");
    saveDraft("chat:42", "already here");
    moveDraft("chat:new", "chat:42");
    expect(loadDraft("chat:42")).toBe("already here");
    expect(loadDraft("chat:new")).toBe("incoming");
  });

  test("moving a key onto itself keeps the draft", () => {
    saveDraft("chat:42", "stay");
    moveDraft("chat:42", "chat:42");
    expect(loadDraft("chat:42")).toBe("stay");
  });
});
