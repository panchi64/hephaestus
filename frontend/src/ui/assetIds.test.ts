import { describe, expect, test } from "bun:test";
import { ASSET_IDS } from "./assetIds";

describe("ASSET_IDS", () => {
  const ids = Object.values(ASSET_IDS);

  test("every id follows HEPH-<SUBSYSTEM>-<SEQ>", () => {
    for (const id of ids) expect(id).toMatch(/^HEPH-[A-Z]{2,4}-(\d{2}|\d{3})$/);
  });

  test("no id repeats", () => {
    expect(new Set(ids).size).toBe(ids.length);
  });
});
