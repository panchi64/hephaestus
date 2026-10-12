/**
 * The arithmetic between the operator's hands and the page they are driving.
 *
 * The frame is an image shown `object-fit: contain` in whatever box the pane gives it,
 * so it is letterboxed, and a pointer position on the element is not a position on the
 * page. Everything here is pure so the mapping can be checked without a browser.
 */

import type { MouseButton } from "../data/browserInput";

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface FrameSize {
  width: number;
  height: number;
}

export interface PagePoint {
  /** Page CSS pixels, clamped to the page. */
  x: number;
  y: number;
  /** Whether the pointer was over the page rather than the letterbox bars. */
  inside: boolean;
}

/** Map a client position to a page position, given the element's box and the page
 *  viewport the frame depicts. Null while either has no area. */
export function mapPointer(
  box: Box,
  frame: FrameSize,
  clientX: number,
  clientY: number,
): PagePoint | null {
  if (box.width <= 0 || box.height <= 0) return null;
  if (frame.width <= 0 || frame.height <= 0) return null;
  const scale = Math.min(box.width / frame.width, box.height / frame.height);
  const shownW = frame.width * scale;
  const shownH = frame.height * scale;
  const offsetX = box.left + (box.width - shownW) / 2;
  const offsetY = box.top + (box.height - shownH) / 2;
  const rawX = (clientX - offsetX) / scale;
  const rawY = (clientY - offsetY) / scale;
  const inside =
    rawX >= 0 && rawX <= frame.width && rawY >= 0 && rawY <= frame.height;
  return {
    x: Math.min(Math.max(rawX, 0), frame.width),
    y: Math.min(Math.max(rawY, 0), frame.height),
    inside,
  };
}

export const MODIFIER_ALT = 1;
export const MODIFIER_CTRL = 2;
export const MODIFIER_META = 4;
export const MODIFIER_SHIFT = 8;

export interface ModifierState {
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

/** The CDP modifier bitmask. */
export function modifierMask(state: ModifierState): number {
  return (
    (state.altKey ? MODIFIER_ALT : 0) |
    (state.ctrlKey ? MODIFIER_CTRL : 0) |
    (state.metaKey ? MODIFIER_META : 0) |
    (state.shiftKey ? MODIFIER_SHIFT : 0)
  );
}

/** `MouseEvent.button` as the wire names it. */
export function mouseButton(button: number): MouseButton {
  if (button === 0) return "left";
  if (button === 1) return "middle";
  if (button === 2) return "right";
  return "none";
}

/** The button a move should report: the one held down, since a drag is a move with a
 *  button. `buttons` is the MouseEvent bitmask (1 left, 2 right, 4 middle). */
export function heldButton(buttons: number): MouseButton {
  if (buttons & 1) return "left";
  if (buttons & 4) return "middle";
  if (buttons & 2) return "right";
  return "none";
}

export interface KeyLike extends ModifierState {
  key: string;
  isComposing?: boolean;
}

/** The character a key press types, or empty for a key that types none. */
export function keyText(event: KeyLike): string {
  if (event.ctrlKey || event.metaKey) return "";
  if (event.key === "Enter") return "\r";
  if (event.key === "Tab") return "\t";
  return [...event.key].length === 1 ? event.key : "";
}

/** The paste chord. It is left to the browser so the `paste` event fires, and the pasted
 *  text is sent whole rather than as keystrokes. */
export function isPasteChord(event: KeyLike): boolean {
  return (
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    event.key.toLowerCase() === "v"
  );
}

/** `WheelEvent` deltas in pixels, whatever unit the device reports them in. */
export function wheelPixels(
  delta: number,
  mode: number,
  pageSize: number,
): number {
  if (mode === 1) return delta * 16;
  if (mode === 2) return delta * pageSize;
  return delta;
}

/** What the address field submits, or null when it is not something to navigate to.
 *  A bare host gets `https://`; only http and https are accepted. */
export function normalizeAddress(input: string): string | null {
  const value = input.trim();
  if (value === "" || /\s/.test(value)) return null;
  const scheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(value);
  if (scheme) {
    return /^https?$/i.test(scheme[1]) && value.length > scheme[0].length
      ? value
      : null;
  }
  if (/^[a-z][a-z0-9+.-]*:(?!\d+(?:[/?#]|$))/i.test(value)) return null;
  return `https://${value}`;
}
