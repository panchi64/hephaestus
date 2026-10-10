import { describe, expect, test } from "bun:test";
import { branchWanted } from "./branchWanted";

describe("branchWanted", () => {
  test("a code thread loaded for this id asks", () => {
    expect(branchWanted("a", { id: "a", mode: "code" })).toBe("a");
  });

  test("sandbox threads never ask", () => {
    expect(branchWanted("a", { id: "a", mode: "normal" })).toBeNull();
    expect(branchWanted("a", { id: "a", mode: "research" })).toBeNull();
  });

  test("a mode loaded for a different thread does not count", () => {
    expect(branchWanted("b", { id: "a", mode: "code" })).toBeNull();
  });

  test("nothing asks before a thread has loaded or while staging", () => {
    expect(branchWanted("a", null)).toBeNull();
    expect(branchWanted(null, { id: "a", mode: "code" })).toBeNull();
  });
});
