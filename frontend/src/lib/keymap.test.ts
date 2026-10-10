import { describe, expect, test } from "bun:test";
import {
  modeShortcutCombo,
  modeShortcutLabel,
} from "~/features/chat/modeShortcuts";
import { combosFor, type KeyEventLike } from "./keymap";

const ev = (over: Partial<KeyEventLike>): KeyEventLike => ({
  key: "",
  code: "",
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  ...over,
});

// Shift turns "1" into "!" — the produced key no longer names the chord.
const ctrlShift2 = ev({
  key: "@",
  code: "Digit2",
  ctrlKey: true,
  shiftKey: true,
});

describe("mode shortcuts", () => {
  test("Ctrl+Shift+digit matches on the physical key, on mac and elsewhere", () => {
    expect(combosFor(ctrlShift2, true)).toContain(modeShortcutCombo(1));
    expect(combosFor(ctrlShift2, false)).toContain(modeShortcutCombo(1));
  });

  test("the old mod+digit chords no longer match the mode combos", () => {
    const mac = ev({ key: "1", code: "Digit1", metaKey: true });
    const win = ev({ key: "1", code: "Digit1", ctrlKey: true });
    expect(combosFor(mac, true)).not.toContain(modeShortcutCombo(0));
    expect(combosFor(win, false)).not.toContain(modeShortcutCombo(0));
  });

  test("Command+Shift+digit on mac is not the chord", () => {
    const e = ev({ key: "!", code: "Digit1", metaKey: true, shiftKey: true });
    expect(combosFor(e, true)).not.toContain(modeShortcutCombo(0));
  });

  test("a bare modifier press yields no combo", () => {
    expect(combosFor(ev({ key: "Control", ctrlKey: true }), false)).toEqual([
      "",
    ]);
  });

  test("existing mod+key bindings still match", () => {
    expect(
      combosFor(
        ev({ key: "V", code: "KeyV", metaKey: true, shiftKey: true }),
        true,
      ),
    ).toContain("mod+shift+v");
    expect(
      combosFor(ev({ key: "p", code: "KeyP", ctrlKey: true }), false),
    ).toContain("mod+p");
  });

  test("tooltip labels", () => {
    expect(modeShortcutLabel(0, true)).toBe("⌃⇧1");
    expect(modeShortcutLabel(2, false)).toBe("Ctrl+Shift+3");
  });
});
