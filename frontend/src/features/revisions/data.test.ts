import { describe, expect, it } from "bun:test";
import { patchStats, revisionsBase, toFileChange, toRevision } from "./data";

describe("toRevision", () => {
  it("renames the snake_case wire fields", () => {
    expect(
      toRevision({
        number: 3,
        note: "published",
        size_bytes: 120,
        created_at: "2026-01-02T03:04:05Z",
      }),
    ).toEqual({
      number: 3,
      note: "published",
      sizeBytes: 120,
      createdAt: "2026-01-02T03:04:05Z",
    });
  });
});

describe("toFileChange", () => {
  it("keeps a null patch for binary files", () => {
    const change = toFileChange({
      path: "logo.png",
      status: "changed",
      patch: null,
    });
    expect(change).toEqual({
      path: "logo.png",
      status: "changed",
      patch: null,
    });
  });
});

describe("patchStats", () => {
  it("counts hunk lines and ignores the file header", () => {
    const patch = [
      "--- a/SKILL.md",
      "+++ b/SKILL.md",
      "@@ -1,3 +1,3 @@",
      " keep",
      "-old",
      "+new",
      "+extra",
    ].join("\n");
    expect(patchStats(patch)).toEqual({ added: 2, removed: 1 });
  });

  it("counts nothing for an empty patch", () => {
    expect(patchStats("")).toEqual({ added: 0, removed: 0 });
  });
});

describe("revisionsBase", () => {
  it("encodes the id", () => {
    expect(revisionsBase("agents", "a b")).toBe("/agents/a%20b/revisions");
    expect(revisionsBase("skills", "x")).toBe("/skills/x/revisions");
  });
});
