import { describe, expect, test } from "bun:test";
import { clearedMessage, openThreadCleared } from "./clearData";

describe("clearedMessage", () => {
  test("counts with the right noun", () => {
    expect(clearedMessage("memories", { deleted: 1, skippedIds: [] })).toBe(
      "Deleted 1 memory.",
    );
    expect(clearedMessage("uploads", { deleted: 0, skippedIds: [] })).toBe(
      "Deleted 0 uploads.",
    );
  });

  test("says how many busy chats were left", () => {
    expect(clearedMessage("chats", { deleted: 12, skippedIds: ["c1"] })).toBe(
      "Deleted 12 chats. 1 still running, left in place.",
    );
  });
});

describe("openThreadCleared", () => {
  test("the open thread went unless it was skipped", () => {
    expect(openThreadCleared("c1", { deleted: 3, skippedIds: [] })).toBe(true);
    expect(openThreadCleared("c1", { deleted: 3, skippedIds: ["c1"] })).toBe(
      false,
    );
  });

  test("no open thread is never cleared", () => {
    expect(openThreadCleared(null, { deleted: 3, skippedIds: [] })).toBe(false);
  });
});
