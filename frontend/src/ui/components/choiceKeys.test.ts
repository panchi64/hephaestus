import { describe, expect, test } from "bun:test";
import { choiceHints, numberKeyIndex, stepIndex } from "./choiceKeys";

describe("numberKeyIndex", () => {
  test("a number key picks its row, zero-based", () => {
    expect(numberKeyIndex("1", 3)).toBe(0);
    expect(numberKeyIndex("3", 3)).toBe(2);
  });

  test("a number past the last row picks nothing", () => {
    expect(numberKeyIndex("4", 3)).toBeNull();
  });

  test("only 1–9 are keys — 0, multi-digit and non-digits pick nothing", () => {
    expect(numberKeyIndex("0", 12)).toBeNull();
    expect(numberKeyIndex("10", 12)).toBeNull();
    expect(numberKeyIndex(" ", 3)).toBeNull();
    expect(numberKeyIndex("a", 3)).toBeNull();
  });

  test("past nine rows the tenth has no key, the ninth still does", () => {
    expect(numberKeyIndex("9", 12)).toBe(8);
  });
});

describe("stepIndex", () => {
  test("moves by one and wraps at both ends", () => {
    expect(stepIndex(0, 1, 3)).toBe(1);
    expect(stepIndex(2, 1, 3)).toBe(0);
    expect(stepIndex(0, -1, 3)).toBe(2);
  });

  test("an empty list has nowhere to go", () => {
    expect(stepIndex(0, 1, 0)).toBe(-1);
  });
});

describe("choiceHints", () => {
  test("names the number range, the arrows and free text", () => {
    expect(choiceHints(2, true)).toBe(
      "1–2 pick · ↑↓ move · type to answer freely",
    );
  });

  test("a single answer has no range and no arrows", () => {
    expect(choiceHints(1, false)).toBe("1 pick");
  });

  test("the range stops at nine keys", () => {
    expect(choiceHints(12, false)).toBe("1–9 pick · ↑↓ move");
  });

  test("free text alone still says so", () => {
    expect(choiceHints(0, true)).toBe("type to answer freely");
  });
});
