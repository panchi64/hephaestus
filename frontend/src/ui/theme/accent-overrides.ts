import { createSignal } from "solid-js";
import { SESSION_MODE_IDS, isSessionMode, type SessionMode } from "~/lib/modes";
import { isServer } from "solid-js/web";
import { readLS, removeLS, writeLS } from "~/lib/storage";
import { sanitizeAxis } from "./accent-axis";
import {
  ACCENT_TOKEN_NAMES,
  hasSessionSignature,
  isAccentToken,
} from "./accents";
import type { AccentToken } from "./accents";
import { normalizeHex } from "./contrast";
import type { ThemeMode } from "./theme-store";

/**
 * The operator's accent overrides: the stored shape, the guard over it, and the
 * one stylesheet it becomes. Sits beside `theme-store` because it is the same
 * kind of state — UI-owned, device-local, and applied to the cascade rather than
 * held by the backend.
 *
 * **The overrides reach the cascade through one `<style>` element, not through
 * inline styles on `<html>`.** That is the whole design, and it follows from one
 * fact: the overrides are *per theme mode*, and the theme changes at runtime — a
 * `"system"` preference flips with the OS while the page is open. An inline
 * style can only hold one theme's values, so every flip would have to rewrite
 * it, and any moment the two disagreed would paint the wrong palette. A
 * stylesheet carrying both rules lets the existing `[data-theme]` switch do the
 * selecting, which means `applyTheme` needs no knowledge of accents at all.
 *
 * The same trick carries the second axis. `data-mode` on the root says which
 * kind of thread is open (`session-mode.ts`), and the sheet emits
 * `html[data-theme=…][data-mode=…]` rules for the signature token — so switching
 * threads repaints the signature with no code running at all.
 *
 * `html[data-theme="…"]` is specificity (0,1,1) and the session rule (0,2,1),
 * which beat tokens.css's `:root` / `[data-theme="paper"]` (0,1,0) and its
 * session block (0,2,0) regardless of where the sheet lands in source order — so
 * none of this depends on load timing.
 *
 * Because theme.css maps the tokens with Tailwind's `@theme inline`, overriding
 * the raw `--accent*` custom property is enough to move everything downstream:
 * every `text-*`/`bg-*` utility, `--shadow-accent`/`--shadow-alert` (which are
 * `color-mix`es of `--accent`), the reasoning wall's tint, and the action tier's
 * band and tint. Nothing else needs to know this feature exists.
 *
 * The per-axis accessors live next door — `accent-store.ts` for the five accent
 * meanings, `session-accent-store.ts` for the signature per session mode. Both
 * write through `update` here, because there is one sheet and one stored blob:
 * two modules each owning half of a single `<style>` element would leave the
 * winner up to whichever wrote last.
 */

export const ACCENT_STORAGE_KEY = "odysseus:accents";

/** The id the pre-paint script in `entry-server.tsx` gives its element. The
 *  store *adopts* that element rather than appending a second one — two sheets
 *  both setting `--accent` would leave the winner up to source order. */
const STYLE_ELEMENT_ID = "ody-accent-overrides";

/** The theme modes, spelled out so the emitted sheet has a stable order and
 *  nothing unvetted can reach it. Named `THEMES` rather than `MODES` now that a
 *  second axis also calls itself a mode. */
export const THEMES: readonly ThemeMode[] = ["phosphor", "paper"];

/** The reserved key the session-mode signatures live under. It cannot collide
 *  with a `ThemeMode`, which is what lets the shape grow without a stored-format
 *  migration — a blob written before this axis existed still loads, and its five
 *  tokens still apply. */
export const SESSION_KEY = "sessionAccent";

/** The signature accent per (theme, session mode). Sparse like everything else
 *  here, and confined to `accent` on purpose — see `SESSION_ACCENT_DEFAULTS`.
 *  Normal is absent by construction: it *is* the base token, so overriding it
 *  there is what changes it. */
export type SessionAccentOverrides = Partial<
  Record<ThemeMode, Partial<Record<SessionMode, string>>>
>;

/** Sparse by design: only what the operator actually changed is stored, so a
 *  token left alone keeps following the shipped palette if that palette is ever
 *  retuned.
 *
 *  Two axes, side by side rather than nested: the five accent *meanings* per
 *  theme, and — under a reserved key — the signature accent per session mode.
 *  Growing the existing per-theme record instead would have meant a key that is
 *  sometimes a token and sometimes a mode, which `isAccentToken` could no longer
 *  guard on its own. */
export type AccentOverrides = Partial<
  Record<ThemeMode, Partial<Record<AccentToken, string>>>
> & { [SESSION_KEY]?: SessionAccentOverrides };

/** Where each axis lives in the stored blob, and which keys it will accept. The two
 *  descriptions are the whole of what separates the axes; the walking, the pruning and
 *  the clear-on-default rule are `accent-axis.ts`'s, written once. */
export const BASE_AXIS = {
  path: (theme: ThemeMode) => [theme] as const,
  accepts: isAccentToken,
};

export const SESSION_AXIS = {
  path: (theme: ThemeMode) => [SESSION_KEY, theme] as const,
  // Never Normal: its value *is* the base token, so it has no rule of its own to
  // write and a stored one would be a second claim on the same declaration.
  accepts: (key: string): key is SessionMode =>
    isSessionMode(key) && hasSessionSignature(key),
};

/** Drop anything that isn't a known key holding a real hex colour, on either axis.
 *  This runs over `localStorage`, which is user-writable and is concatenated into a
 *  stylesheet below — so the guard is load-bearing, not tidiness. */
export function sanitize(raw: unknown): AccentOverrides {
  const base = sanitizeAxis(raw, {}, THEMES, BASE_AXIS);
  return sanitizeAxis(raw, base, THEMES, SESSION_AXIS);
}

function readStored(): AccentOverrides {
  if (isServer) return {};
  try {
    return sanitize(JSON.parse(readLS(ACCENT_STORAGE_KEY) ?? "{}"));
  } catch {
    // Only `JSON.parse` can throw here — `readLS` answers null when storage is
    // unavailable — and a blob that isn't JSON is a blob with nothing to honour.
    return {};
  }
}

const [overrides, setOverridesSignal] =
  createSignal<AccentOverrides>(readStored());

/** The CSS the override sheet holds. Exported so a test can assert its shape
 *  without a DOM — it is the one piece of this module that is pure. */
export function serializeOverrides(value: AccentOverrides): string {
  let css = "";
  for (const theme of THEMES) {
    const perTheme = value[theme];
    if (!perTheme) continue;
    let body = "";
    // Iterate the known token list rather than the object's own keys, so the
    // emitted order is stable and nothing unvetted can reach the sheet.
    for (const token of ACCENT_TOKEN_NAMES) {
      const hex = perTheme[token] ? normalizeHex(perTheme[token]!) : null;
      if (hex) body += `--${token}:${hex};`;
    }
    if (body) css += `html[data-theme="${theme}"]{${body}}`;
  }
  // The session rules are more specific — (0,2,1) against (0,1,1) — so a mode
  // signature wins over a hand-set base accent in the same theme on specificity
  // rather than on which of the two happened to be written last.
  for (const theme of THEMES) {
    const perTheme = value[SESSION_KEY]?.[theme];
    if (!perTheme) continue;
    for (const sessionMode of SESSION_MODE_IDS) {
      if (!hasSessionSignature(sessionMode)) continue;
      const raw = perTheme[sessionMode];
      const hex = raw ? normalizeHex(raw) : null;
      if (!hex) continue;
      css += `html[data-theme="${theme}"][data-mode="${sessionMode}"]{--accent:${hex};}`;
    }
  }
  return css;
}

function applyOverrides(value: AccentOverrides): void {
  if (isServer || typeof document === "undefined") return;
  const css = serializeOverrides(value);
  let el = document.getElementById(STYLE_ELEMENT_ID);
  if (!el) {
    // Nothing to say and nothing already saying it — don't add an empty sheet.
    if (!css) return;
    el = document.createElement("style");
    el.id = STYLE_ELEMENT_ID;
    document.head.appendChild(el);
  }
  el.textContent = css;
}

function persist(value: AccentOverrides): void {
  // The guarded access itself is `lib/storage`'s — storage can throw (private mode,
  // blocked, full) and the palette is best-effort either way, like the draft cache.
  if (isServer) return;
  if (Object.keys(value).length > 0)
    writeLS(ACCENT_STORAGE_KEY, JSON.stringify(value));
  else removeLS(ACCENT_STORAGE_KEY);
}

/** Repaint now; write to storage only if asked. The picker drags through
 *  hundreds of `input` events, and every one of them should be visible while
 *  none of them should hit `localStorage` — the caller commits on `change`. */
export function update(
  next: AccentOverrides,
  options?: { persist?: boolean },
): void {
  setOverridesSignal(next);
  applyOverrides(next);
  if (options?.persist !== false) persist(next);
}

/** Whether anything at all is overridden, in any theme — on either axis, so the
 *  RESET ALL control appears for a hand-set code accent as readily as for a
 *  hand-set alert red. */
export function hasAccentOverrides(): boolean {
  const value = overrides();
  return THEMES.some(
    (theme) =>
      Object.keys(value[theme] ?? {}).length > 0 ||
      Object.keys(value[SESSION_KEY]?.[theme] ?? {}).length > 0,
  );
}

/** Clear every override in every theme. Returns what was cleared, so the caller
 *  can offer UNDO without holding its own copy. */
export function resetAllAccents(): AccentOverrides {
  const previous = overrides();
  update({});
  return previous;
}

/** Put a whole override set back — the other half of UNDO. */
export function restoreAccents(value: AccentOverrides): void {
  update(sanitize(value));
}

/** Reactive accessor, for anything that needs to re-render on a palette change. */
export { overrides as accentOverrides };
