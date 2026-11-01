import { describe, expect, test } from "bun:test";
import { clipText } from "./clipText";

describe("clipText", () => {
  test("leaves text that fits alone", () => {
    expect(clipText("short", 10)).toBe("short");
    expect(clipText("exactly10!", 10)).toBe("exactly10!");
  });

  test("cuts at the last space near the limit", () => {
    expect(clipText("the quick brown fox jumps", 22)).toBe(
      "the quick brown fox…",
    );
  });

  test("cuts mid-word when no space is near the limit", () => {
    expect(clipText("a supercalifragilistic word", 12)).toBe("a supercalif…");
  });
});
