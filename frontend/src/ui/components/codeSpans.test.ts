import { describe, expect, test } from "bun:test";
import { codeSpans } from "./codeSpans";

describe("codeSpans", () => {
  test("splits code spans out of prose", () => {
    expect(codeSpans("Spawned with `worktree` here.")).toEqual([
      { text: "Spawned with ", code: false },
      { text: "worktree", code: true },
      { text: " here.", code: false },
    ]);
  });

  test("keeps plain text whole", () => {
    expect(codeSpans("No code.")).toEqual([{ text: "No code.", code: false }]);
  });

  test("leaves an unmatched backtick literal", () => {
    expect(codeSpans("a `b")).toEqual([{ text: "a `b", code: false }]);
  });

  test("handles adjacent spans", () => {
    expect(codeSpans("`a``b`")).toEqual([
      { text: "a", code: true },
      { text: "b", code: true },
    ]);
  });
});
