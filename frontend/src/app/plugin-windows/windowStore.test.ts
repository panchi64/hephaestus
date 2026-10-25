import { describe, expect, test } from "bun:test";
import {
  closeWindow,
  moveWindow,
  openWindow,
  resizeWindow,
  type WindowSpec,
} from "./windowStore";

const VIEW = { width: 1200, height: 800 };

function spec(window: string, over: Partial<WindowSpec> = {}): WindowSpec {
  return {
    pluginId: "p1",
    plugin: "snake",
    window,
    title: "Snake",
    width: 360,
    height: 420,
    minWidth: 240,
    minHeight: 280,
    resizable: true,
    ...over,
  };
}

describe("plugin windows", () => {
  test("opening an open window raises it instead of a second copy", () => {
    let list = openWindow([], spec("main"), VIEW);
    list = openWindow(list, spec("scores"), VIEW);
    list = openWindow(list, spec("main"), VIEW);
    expect(list.map((w) => w.window)).toEqual(["scores", "main"]);
  });

  test("a new window cascades from the last", () => {
    const list = openWindow(openWindow([], spec("a"), VIEW), spec("b"), VIEW);
    expect(list[1].x).toBeLessThan(list[0].x);
    expect(list[1].y).toBeGreaterThan(list[0].y);
  });

  test("a window dragged off screen keeps a corner to grab", () => {
    const list = openWindow([], spec("main"), VIEW);
    const [moved] = moveWindow(list, list[0].key, 5000, 5000, VIEW);
    expect(moved.x).toBeLessThan(VIEW.width);
    expect(moved.y).toBeLessThan(VIEW.height);
    const [back] = moveWindow(list, list[0].key, -5000, -5000, VIEW);
    expect(back.x + back.width).toBeGreaterThan(0);
    expect(back.y).toBe(0);
  });

  test("resizing stops at the minimum, and not at all when fixed", () => {
    const list = openWindow([], spec("main"), VIEW);
    const [small] = resizeWindow(list, list[0].key, -1000, -1000, VIEW);
    expect([small.width, small.height]).toEqual([240, 280]);
    const fixed = openWindow([], spec("main", { resizable: false }), VIEW);
    const [same] = resizeWindow(fixed, fixed[0].key, 100, 100, VIEW);
    expect([same.width, same.height]).toEqual([360, 420]);
  });

  test("closing removes only that window", () => {
    const list = openWindow(openWindow([], spec("a"), VIEW), spec("b"), VIEW);
    expect(closeWindow(list, list[0].key).map((w) => w.window)).toEqual(["b"]);
  });
});
