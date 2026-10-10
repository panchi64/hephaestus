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

/** Normalizes a keydown event to the same combo format bindings are registered
 *  with. */
function comboFor(e: KeyboardEvent): string {
  const parts: string[] = [];
  const mod = isMac() ? e.metaKey : e.ctrlKey;
  if (mod) parts.push("mod");
  if (e.shiftKey) parts.push("shift");
  if (e.altKey) parts.push("alt");
  const key = e.key.toLowerCase();
  if (!NON_TRIGGER_KEYS.has(key)) parts.push(key);
  return parts.join("+");
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
  const combo = comboFor(e);
  // A combo with no "+" carries no modifier — ignored while typing.
  const isModifierless = !combo.includes("+");
  if (isModifierless && isEditableTarget(e.target)) return;

  for (const entry of registry) {
    for (const binding of entry.bindings()) {
      if (binding.combo !== combo) continue;
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
