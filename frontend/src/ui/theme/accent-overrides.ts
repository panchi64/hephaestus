import { createSignal } from "solid-js";
import { isServer } from "solid-js/web";
import { readLS, removeLS, writeLS } from "~/lib/storage";
import { sanitizeAxis } from "./accent-axis";
import { ACCENT_TOKEN_NAMES, isAccentToken } from "./accents";
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
 * `html[data-theme="…"]` is specificity (0,1,1), which beats tokens.css's `:root` /
 * `[data-theme="paper"]` (0,1,0) regardless of where the sheet lands in source order — so
 * none of this depends on load timing.
 *
 * Because theme.css maps the tokens with Tailwind's `@theme inline`, overriding
 * the raw `--accent*` custom property is enough to move everything downstream:
 * every `text-*`/`bg-*` utility, `--shadow-accent`/`--shadow-alert` (which are
 * `color-mix`es of `--accent`), the reasoning wall's tint, and the action tier's
 * band and tint. Nothing else needs to know this feature exists.
 *
 * The accessors live next door in `accent-store.ts`; they write through `update` here,
 * because there is one sheet and one stored blob.
 */

export const ACCENT_STORAGE_KEY = "odysseus:accents";

/** The id the pre-paint script in `entry-server.tsx` gives its element. The
 *  store *adopts* that element rather than appending a second one — two sheets
 *  both setting `--accent` would leave the winner up to source order. */
const STYLE_ELEMENT_ID = "ody-accent-overrides";

/** The theme modes, spelled out so the emitted sheet has a stable order and
 *  nothing unvetted can reach it. */
export const THEMES: readonly ThemeMode[] = ["phosphor", "paper"];

/** Sparse by design: only what the operator actually changed is stored, so a
 *  token left alone keeps following the shipped palette if that palette is ever
 *  retuned. */
export type AccentOverrides = Partial<
  Record<ThemeMode, Partial<Record<AccentToken, string>>>
>;

/** Drop anything that isn't a known token holding a real hex colour.
 *  This runs over `localStorage`, which is user-writable and is concatenated into a
 *  stylesheet below — so the guard is load-bearing, not tidiness. */
export function sanitize(raw: unknown): AccentOverrides {
  return sanitizeAxis(raw, THEMES, isAccentToken);
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

/** Whether anything at all is overridden, in any theme, so the RESET ALL control
 *  appears. */
export function hasAccentOverrides(): boolean {
  const value = overrides();
  return THEMES.some((theme) => Object.keys(value[theme] ?? {}).length > 0);
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
