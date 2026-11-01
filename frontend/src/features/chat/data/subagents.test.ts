import { describe, expect, test } from "bun:test";
import { designationText, modelText } from "./subagents";

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

describe("a sub-agent's model line", () => {
  test("names the model and what chose it", () => {
    expect(modelText({ model: "local/qwen3", modelSource: "pin" })).toBe(
      "local/qwen3 · pinned",
    );
    expect(modelText({ model: "cloud/opus", modelSource: "spawn" })).toBe(
      "cloud/opus · chosen at launch",
    );
  });

  test("is empty on a row written before models were recorded", () => {
    expect(modelText({ model: "", modelSource: "" })).toBe("");
  });
});
