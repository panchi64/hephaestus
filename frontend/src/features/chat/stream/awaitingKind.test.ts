import { describe, expect, test } from "bun:test";
import { awaitingKindOf } from "./awaitingKind";

describe("awaitingKindOf", () => {
  test("nothing parked waits on nothing", () => {
    expect(awaitingKindOf(null, false)).toBeNull();
  });

  test("approvals alone are an approval", () => {
    expect(awaitingKindOf({ questions: [], stale: false }, false)).toBe(
      "approval",
    );
  });

  test("a question in the park makes it a question", () => {
    expect(awaitingKindOf({ questions: [{}], stale: false }, false)).toBe(
      "question",
    );
  });

  test("a stale park waits on nothing, even beside a pending command", () => {
    expect(awaitingKindOf({ questions: [{}], stale: true }, true)).toBeNull();
  });

  test("a pending host command is an approval", () => {
    expect(awaitingKindOf(null, true)).toBe("approval");
  });
});
