import { describe, expect, test } from "bun:test";
import { createArrivalTracker } from "./arrivals";

describe("createArrivalTracker", () => {
  test("the seed never arrives", () => {
    const next = createArrivalTracker(["a", "b"]);
    expect([...next(["a", "b"])]).toEqual([]);
  });

  test("reports only the ids added since the last reading", () => {
    const next = createArrivalTracker(["a"]);
    expect([...next(["a", "b"])]).toEqual(["b"]);
    expect([...next(["a", "b", "c", "d"])]).toEqual(["c", "d"]);
  });

  test("a reading with nothing new reports nothing", () => {
    const next = createArrivalTracker([]);
    next(["a"]);
    expect([...next(["a"])]).toEqual([]);
  });

  test("an id that leaves and returns is not an arrival", () => {
    const next = createArrivalTracker(["a"]);
    next(["a", "b"]);
    next(["a"]);
    expect([...next(["a", "b"])]).toEqual([]);
  });
});
