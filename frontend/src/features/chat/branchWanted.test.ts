import { describe, expect, test } from "bun:test";
import { branchWanted } from "./branchWanted";

describe("branchWanted", () => {
  test("a worktree thread asks", () => {
    expect(branchWanted("a", "worktree")).toBe("a");
  });

  test("sandbox threads never ask", () => {
    expect(branchWanted("a", "sandbox")).toBeNull();
  });

  test("nothing asks before the thread's kind is known or while staging", () => {
    expect(branchWanted("a", undefined)).toBeNull();
    expect(branchWanted(null, "worktree")).toBeNull();
  });
});
