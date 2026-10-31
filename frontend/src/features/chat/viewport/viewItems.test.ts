import { describe, expect, test } from "bun:test";
import { detectContentKind } from "./viewItems";

describe("detectContentKind", () => {
  test("a .md file renders as markdown", () => {
    expect(detectContentKind("EU AI Act startup obligations.md", "text")).toBe(
      "markdown",
    );
  });

  test("a .txt file stays plain text", () => {
    expect(detectContentKind("notes.txt", "text")).toBe("text");
  });

  test("a version titled without an extension falls back to its preview kind", () => {
    expect(detectContentKind("Research report", "text")).toBe("text");
    expect(detectContentKind("Research report", "markdown")).toBe("markdown");
  });
});
