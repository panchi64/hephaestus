import { describe, expect, test } from "bun:test";
import { pendingCount, type ChangedFile } from "./changes";

const file = (id: string, reverted = false): ChangedFile => ({
  id,
  path: `${id}.py`,
  status: "modified",
  reverted,
  revertible: true,
});

describe("pendingCount", () => {
  test("counts what is still changed across every project", () => {
    expect(
      pendingCount({
        worktree: false,
        projects: [
          {
            projectId: "a",
            name: "a",
            homePath: "",
            files: [file("1"), file("2", true)],
          },
          { projectId: "b", name: "b", homePath: "", files: [file("3")] },
        ],
      }),
    ).toBe(2);
  });

  test("nothing read yet is nothing pending", () => {
    expect(pendingCount(undefined)).toBe(0);
    expect(pendingCount(null)).toBe(0);
  });
});
