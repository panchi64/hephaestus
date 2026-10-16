import { describe, expect, test } from "bun:test";
import { branchWanted } from "./branchWanted";

describe("branchWanted", () => {
  test("a thread with a worktree asks", () => {
    expect(branchWanted("a", true)).toBe("a");
  });

  test("a thread without one never asks", () => {
    expect(branchWanted("a", false)).toBeNull();
  });

  test("nothing asks before the thread's answer is known or while staging", () => {
    expect(branchWanted("a", undefined)).toBeNull();
    expect(branchWanted(null, true)).toBeNull();
  });
});
