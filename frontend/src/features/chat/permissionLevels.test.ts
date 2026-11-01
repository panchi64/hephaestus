import { describe, expect, test } from "bun:test";
import {
  nextPermissionLevel,
  parsePermissionLevel,
  PERMISSION_LEVELS,
} from "./model";

describe("nextPermissionLevel — the composer's Shift+Tab cycle", () => {
  test("walks the list in its declared order", () => {
    expect(nextPermissionLevel("plan")).toBe("manual");
    expect(nextPermissionLevel("manual")).toBe("edit");
    expect(nextPermissionLevel("edit")).toBe("auto");
  });

  test("never lands on yolo — it wraps past it to the strictest level", () => {
    // The widest level is reachable only by naming it; a reflex keystroke must not be
    // how a thread gets there.
    expect(nextPermissionLevel("auto")).toBe("plan");
  });

  test("from yolo, the cycle leaves to the strictest level", () => {
    expect(nextPermissionLevel("yolo")).toBe("plan");
  });

  test("a full lap visits every level but yolo, once", () => {
    const seen: string[] = [];
    let level = nextPermissionLevel("yolo");
    for (let i = 0; i < PERMISSION_LEVELS.length - 1; i++) {
      seen.push(level);
      level = nextPermissionLevel(level);
    }
    expect(seen).toEqual(["plan", "manual", "edit", "auto"]);
    expect(level).toBe("plan");
  });
});

describe("parsePermissionLevel — the word `/level` is given", () => {
  test("accepts an id", () => {
    expect(parsePermissionLevel("manual")).toBe("manual");
  });

  test("accepts the label the control shows, in any case", () => {
    expect(parsePermissionLevel("Ask first")).toBe("manual");
    expect(parsePermissionLevel("ask FIRST")).toBe("manual");
    expect(parsePermissionLevel("no limits")).toBe("yolo");
    expect(parsePermissionLevel("  Workspace ")).toBe("edit");
  });

  test("an id in capitals is still the id", () => {
    expect(parsePermissionLevel("PLAN")).toBe("plan");
  });

  test("anything else is null", () => {
    expect(parsePermissionLevel("everything")).toBeNull();
    expect(parsePermissionLevel("")).toBeNull();
  });
});
