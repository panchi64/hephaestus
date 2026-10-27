import { describe, expect, test } from "bun:test";
import { calendarMarkPattern } from "./model";

describe("calendarMarkPattern", () => {
  test("gives each tone a distinct pattern", () => {
    const tones = ["nominal", "info", "warn", "alert"] as const;
    const patterns = tones.map(calendarMarkPattern);
    expect(patterns).toEqual(["filled", "hollow", "hatched", "crossed"]);
    expect(new Set(patterns).size).toBe(tones.length);
  });
});
