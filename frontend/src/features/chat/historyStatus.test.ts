import { describe, expect, test } from "bun:test";
import { historyStatus, type ResourceState } from "./historyStatus";

/**
 * What the transcript is told while a thread's history is on its way, and after it
 * fails. The case that started this: every one of these used to read as "nothing said
 * yet", and a failed read stayed that way for good.
 */

const read = (
  state: ResourceState,
  holdsThisThread = false,
  id: string | null = "c1",
) => historyStatus({ id, state, holdsThisThread });

describe("the open thread's history", () => {
  test("a thread with no id has nothing to read", () => {
    expect(read("ready", true, null)).toBe("idle");
    expect(read("pending", false, null)).toBe("idle");
  });

  test("is loading from the click until the answer lands, not empty", () => {
    expect(read("unresolved")).toBe("loading");
    expect(read("pending")).toBe("loading");
  });

  test("a value the resource kept from the last thread is not this thread's", () => {
    // Solid retains the previous value across a source change, so `ready` alone would
    // seat the thread just left under the one just opened.
    expect(read("ready", false)).toBe("loading");
    expect(read("refreshing", true)).toBe("loading");
  });

  test("is ready once the answer is this thread's own", () => {
    expect(read("ready", true)).toBe("ready");
  });

  test("a failed read is an error, which is what offers the retry", () => {
    expect(read("errored")).toBe("error");
  });

  test("load, fail, retry, succeed — each step reads as itself", () => {
    // The retry bumps the resource's revision, which puts it back to pending.
    const steps: [ResourceState, boolean][] = [
      ["pending", false],
      ["errored", false],
      ["pending", false],
      ["ready", true],
    ];
    expect(steps.map(([state, holds]) => read(state, holds))).toEqual([
      "loading",
      "error",
      "loading",
      "ready",
    ]);
  });
});
