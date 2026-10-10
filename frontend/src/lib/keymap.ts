/** One shared `document` keydown listener the whole app-side keymap registers
 *  bindings against, instead of every component attaching its own. Presentation
 *  only — a binding's `run()` relays intent (e.g. "toggle the panel"); it never
 *  decides anything itself. */

import { onCleanup } from "solid-js";

/** `combo` is lowercase, e.g. `"mod+shift+v"`, `"["`, `"p"`, `"escape"`. `"mod"` is
 *  `metaKey` on macOS, `ctrlKey` elsewhere. `when` gates the binding (checked only
 *  on a combo match); `run` is the effect. */
export interface KeyBinding {
  combo: string;
  when?: () => boolean;
  run: () => void;
}

interface KeymapEntry {
  bindings: () => KeyBinding[];
}

const registry: KeymapEntry[] = [];
let attached = false;

/** `navigator.userAgentData` isn't in the standard TS lib yet — a minimal
 *  structural type instead of `any` keeps this typed without widening. */
type NavigatorWithUAData = Navigator & {
  userAgentData?: { platform?: string };
};

/** Whether `mod` is ⌘ here — for a hint that prints the chord. */
export function isMac(): boolean {
  if (typeof navigator === "undefined") return false;
  const uaPlatform = (navigator as NavigatorWithUAData).userAgentData?.platform;
  if (uaPlatform !== undefined) return /mac/i.test(uaPlatform);
  return /mac/i.test(navigator.platform || navigator.userAgent);
}

const NON_TRIGGER_KEYS = new Set(["meta", "control", "shift", "alt"]);

/** The fields of a keydown the combo derivation reads — a structural subset, so the
 *  pure derivation is testable without a DOM. */
export type KeyEventLike = Pick<
  KeyboardEvent,
  "key" | "code" | "metaKey" | "ctrlKey" | "shiftKey" | "altKey"
>;

/** Every spelling of a keydown a binding may have been registered with.
 *
 *  `mod+shift+v` matches on the produced key (`event.key`). A combo whose last part is
 *  `code:digit1` matches the *physical* key instead (`event.code`), which is what a
 *  chord with Shift needs — Shift turns `1` into `!`, so `key` no longer names it. A
 *  literal `ctrl` is the Control key on every platform, where `mod` is ⌘ on macOS and
 *  Control elsewhere; off macOS the two are the same key and both spellings match. */
export function combosFor(e: KeyEventLike, mac: boolean): string[] {
  const key = e.key.toLowerCase();
  if (NON_TRIGGER_KEYS.has(key)) return [""];
  const spell = (modifiers: string[], last: string) =>
    [...modifiers, last].join("+");
  const rest: string[] = [];
  if (e.shiftKey) rest.push("shift");
  if (e.altKey) rest.push("alt");
  const mods: string[][] = [];
  if (mac) {
    mods.push([...(e.metaKey ? ["mod"] : []), ...(e.ctrlKey ? ["ctrl"] : [])]);
  } else {
    mods.push(e.ctrlKey ? ["mod"] : []);
    if (e.ctrlKey) mods.push(["ctrl"]);
  }
  const lasts = [key, `code:${e.code.toLowerCase()}`];
  return mods.flatMap((m) => lasts.map((last) => spell([...m, ...rest], last)));
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return (
    tag === "input" ||
    tag === "textarea" ||
    tag === "select" ||
    target.isContentEditable
  );
}

function handleKeydown(e: KeyboardEvent): void {
  // An IME mid-composition owns the keystroke; `keyCode` 229 is the only signal some
  // engines give.
  if (e.isComposing || e.keyCode === 229) return;
  const combos = combosFor(e, isMac());
  // A combo with no "+" carries no modifier — ignored while typing.
  const isModifierless = !combos[0].includes("+");
  if (isModifierless && isEditableTarget(e.target)) return;

  for (const entry of registry) {
    for (const binding of entry.bindings()) {
      if (!combos.includes(binding.combo)) continue;
      if (binding.when && !binding.when()) continue;
      e.preventDefault();
      binding.run();
      return;
    }
  }
}

/** Registers a reactive list of bindings. First match, in registration order
 *  (across all `registerKeymap` callers, then within one caller's list), wins and
 *  preventDefaults. Unregisters automatically on the calling owner's cleanup. */
export function registerKeymap(bindings: () => KeyBinding[]): void {
  const entry: KeymapEntry = { bindings };
  registry.push(entry);

  if (!attached && typeof document !== "undefined") {
    document.addEventListener("keydown", handleKeydown);
    attached = true;
  }

  onCleanup(() => {
    const i = registry.indexOf(entry);
    if (i !== -1) registry.splice(i, 1);
  });
}
