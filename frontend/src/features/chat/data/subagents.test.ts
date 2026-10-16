import { describe, expect, test } from "bun:test";
import { designationText } from "./subagents";

describe("a sub-agent's designation", () => {
  test("reads callsign then class", () => {
    expect(designationText({ callsign: "BRAX", agentClass: "Tripodes" })).toBe(
      "BRAX · Tripodes",
    );
  });

  test("is the callsign alone when the class is unknown", () => {
    expect(designationText({ callsign: "KYRĒ", agentClass: "" })).toBe("KYRĒ");
  });

  test("is empty without a callsign, so callers fall back to the handle", () => {
    expect(designationText({ callsign: "", agentClass: "Tripodes" })).toBe("");
    expect(designationText({})).toBe("");
  });
});
