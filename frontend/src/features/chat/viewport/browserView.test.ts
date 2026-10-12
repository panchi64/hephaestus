import { describe, expect, test } from "bun:test";
import {
  heldButton,
  isPasteChord,
  keyText,
  mapPointer,
  modifierMask,
  mouseButton,
  normalizeAddress,
  wheelPixels,
} from "./browserView";

const none = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false };

describe("mapPointer", () => {
  const frame = { width: 1280, height: 720 };

  test("an exactly fitting box maps one to one", () => {
    const box = { left: 100, top: 50, width: 1280, height: 720 };
    expect(mapPointer(box, frame, 100 + 640, 50 + 360)).toEqual({
      x: 640,
      y: 360,
      inside: true,
    });
  });

  test("scales by the displayed size", () => {
    const box = { left: 0, top: 0, width: 640, height: 360 };
    expect(mapPointer(box, frame, 320, 180)).toEqual({
      x: 640,
      y: 360,
      inside: true,
    });
  });

  test("a tall box letterboxes top and bottom", () => {
    // 640 wide shows the 16:9 page 360 tall, centred in 640 of height.
    const box = { left: 10, top: 20, width: 640, height: 640 };
    const top = 20 + (640 - 360) / 2;
    expect(mapPointer(box, frame, 10, top)).toEqual({
      x: 0,
      y: 0,
      inside: true,
    });
    expect(mapPointer(box, frame, 10 + 640, top + 360)).toEqual({
      x: 1280,
      y: 720,
      inside: true,
    });
    const bar = mapPointer(box, frame, 330, 25);
    expect(bar?.inside).toBe(false);
    expect(bar?.y).toBe(0);
  });

  test("a wide box letterboxes left and right", () => {
    // 360 tall shows the page 640 wide, centred in 1000.
    const box = { left: 0, top: 0, width: 1000, height: 360 };
    const left = (1000 - 640) / 2;
    expect(mapPointer(box, frame, left + 320, 180)).toEqual({
      x: 640,
      y: 360,
      inside: true,
    });
    const bar = mapPointer(box, frame, 10, 180);
    expect(bar?.inside).toBe(false);
    expect(bar?.x).toBe(0);
  });

  test("clamps a position past the page", () => {
    const box = { left: 0, top: 0, width: 640, height: 360 };
    const out = mapPointer(box, frame, 900, 500);
    expect(out).toEqual({ x: 1280, y: 720, inside: false });
  });

  test("an empty box or frame maps nothing", () => {
    expect(
      mapPointer({ left: 0, top: 0, width: 0, height: 10 }, frame, 1, 1),
    ).toBeNull();
    expect(
      mapPointer(
        { left: 0, top: 0, width: 10, height: 10 },
        { width: 0, height: 0 },
        1,
        1,
      ),
    ).toBeNull();
  });
});

describe("modifierMask", () => {
  test("is the CDP bitmask", () => {
    expect(modifierMask(none)).toBe(0);
    expect(modifierMask({ ...none, altKey: true })).toBe(1);
    expect(modifierMask({ ...none, ctrlKey: true })).toBe(2);
    expect(modifierMask({ ...none, metaKey: true })).toBe(4);
    expect(modifierMask({ ...none, shiftKey: true })).toBe(8);
    expect(
      modifierMask({
        altKey: true,
        ctrlKey: true,
        metaKey: true,
        shiftKey: true,
      }),
    ).toBe(15);
    expect(modifierMask({ ...none, ctrlKey: true, shiftKey: true })).toBe(10);
  });
});

describe("pointer and key helpers", () => {
  test("mouse buttons", () => {
    expect(mouseButton(0)).toBe("left");
    expect(mouseButton(1)).toBe("middle");
    expect(mouseButton(2)).toBe("right");
    expect(mouseButton(4)).toBe("none");
    expect(heldButton(0)).toBe("none");
    expect(heldButton(1)).toBe("left");
    expect(heldButton(2)).toBe("right");
    expect(heldButton(4)).toBe("middle");
  });

  test("keyText types printable characters and Enter, not chords", () => {
    expect(keyText({ ...none, key: "a" })).toBe("a");
    expect(keyText({ ...none, key: "A", shiftKey: true })).toBe("A");
    expect(keyText({ ...none, key: "Enter" })).toBe("\r");
    expect(keyText({ ...none, key: "Tab" })).toBe("\t");
    expect(keyText({ ...none, key: "ArrowLeft" })).toBe("");
    expect(keyText({ ...none, key: "a", ctrlKey: true })).toBe("");
    expect(keyText({ ...none, key: "a", metaKey: true })).toBe("");
  });

  test("the paste chord is left to the browser", () => {
    expect(isPasteChord({ ...none, key: "v", ctrlKey: true })).toBe(true);
    expect(isPasteChord({ ...none, key: "V", metaKey: true })).toBe(true);
    expect(isPasteChord({ ...none, key: "v" })).toBe(false);
    expect(isPasteChord({ ...none, key: "c", ctrlKey: true })).toBe(false);
  });

  test("wheel deltas normalise to pixels", () => {
    expect(wheelPixels(3, 0, 800)).toBe(3);
    expect(wheelPixels(3, 1, 800)).toBe(48);
    expect(wheelPixels(1, 2, 800)).toBe(800);
  });
});

describe("normalizeAddress", () => {
  test("keeps http and https as typed", () => {
    expect(normalizeAddress("https://example.com/a?b=1")).toBe(
      "https://example.com/a?b=1",
    );
    expect(normalizeAddress("  http://example.com ")).toBe(
      "http://example.com",
    );
  });

  test("prefixes a bare host", () => {
    expect(normalizeAddress("example.com")).toBe("https://example.com");
    expect(normalizeAddress("example.com/docs")).toBe(
      "https://example.com/docs",
    );
    expect(normalizeAddress("localhost:3000")).toBe("https://localhost:3000");
    expect(normalizeAddress("example.com:8080/x")).toBe(
      "https://example.com:8080/x",
    );
  });

  test("refuses anything that is not a web address", () => {
    expect(normalizeAddress("")).toBeNull();
    expect(normalizeAddress("   ")).toBeNull();
    expect(normalizeAddress("two words")).toBeNull();
    expect(normalizeAddress("javascript:alert(1)")).toBeNull();
    expect(normalizeAddress("file:///etc/passwd")).toBeNull();
    expect(normalizeAddress("ftp://example.com")).toBeNull();
    expect(normalizeAddress("https://")).toBeNull();
  });
});
