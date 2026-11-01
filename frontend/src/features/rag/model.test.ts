import { describe, expect, test } from "bun:test";
import { documentCount, type RagSource } from "./model";

const source = (docCount: number): RagSource => ({
  id: `s${docCount}`,
  kind: "surface",
  label: "Memory",
  icon: "database",
  docCount,
  status: "indexed",
  lastIndexedAt: null,
});

describe("documentCount", () => {
  test("sums every source's documents", () => {
    expect(documentCount([source(4), source(2), source(1)])).toBe(7);
  });

  test("is zero with no sources", () => {
    expect(documentCount([])).toBe(0);
  });
});
