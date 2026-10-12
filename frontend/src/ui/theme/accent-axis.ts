/**
 * The accent overrides' rules, written once: reading and writing one theme's tokens in
 * the stored blob, the clear-on-default rule, and the prune-the-empty-parent unwind.
 *
 * The unwind is the reason this is not inlined in the store. An override cleared back to
 * its shipped value that left an empty object behind would keep `hasAccentOverrides`
 * answering yes, and the RESET ALL control would never go away.
 *
 * Deliberately free of any runtime dependency on `accent-overrides`: the stored blob and
 * the update function are handed in, so the module that owns the storage can use these
 * helpers to sanitize what it reads without the two importing each other.
 */

import type { AccentOverrides } from "./accent-overrides";
import { normalizeHex } from "./contrast";
import type { ThemeMode } from "./theme-store";

/** One theme's map out of an untrusted value, or undefined. Guards every level, because
 *  the same read is used over `localStorage`'s answer — which is user-writable and may be
 *  any shape at all. */
export function readTheme(
  value: unknown,
  theme: ThemeMode,
): Record<string, string> | undefined {
  if (!value || typeof value !== "object") return undefined;
  const node = (value as Record<string, unknown>)[theme];
  return node && typeof node === "object"
    ? (node as Record<string, string>)
    : undefined;
}

/** `value` with one theme's map replaced, **dropping the theme when it is left empty**.
 *  Copies rather than mutates, so the signal sees a new object and repaints. */
export function writeTheme(
  value: AccentOverrides,
  theme: ThemeMode,
  entry: Record<string, string>,
): AccentOverrides {
  const next: AccentOverrides = { ...value };
  if (Object.keys(entry).length > 0) next[theme] = entry;
  else delete next[theme];
  return next;
}

/** What the axis falls back to when nothing is overridden. */
export interface AccentAxisSpec<K extends string> {
  /** The shipped value a key resolves to. Read from the defaults table rather than the
   *  cascade — by the time any of this runs the pre-paint script has applied the
   *  overrides on top, so the cascade holds the *current* value, which is exactly not
   *  what "is this overridden" is asking. */
  shipped: (theme: ThemeMode, key: K) => string;
}

/** The stored blob and the one way to replace it. */
export interface AccentStoreHandle {
  read: () => AccentOverrides;
  update: (next: AccentOverrides, options?: { persist?: boolean }) => void;
}

export interface AccentAxis<K extends string> {
  value: (theme: ThemeMode, key: K) => string;
  isOverridden: (theme: ThemeMode, key: K) => boolean;
  set: (
    theme: ThemeMode,
    key: K,
    value: string,
    options?: { persist?: boolean },
  ) => void;
  reset: (theme: ThemeMode, key: K, options?: { persist?: boolean }) => void;
}

export function createAccentAxis<K extends string>(
  spec: AccentAxisSpec<K>,
  store: AccentStoreHandle,
): AccentAxis<K> {
  const entryOf = (theme: ThemeMode) => readTheme(store.read(), theme);

  const reset: AccentAxis<K>["reset"] = (theme, key, options) => {
    const entry = { ...entryOf(theme) };
    delete entry[key];
    store.update(writeTheme(store.read(), theme, entry), options);
  };

  return {
    value: (theme, key) => entryOf(theme)?.[key] ?? spec.shipped(theme, key),
    isOverridden: (theme, key) => entryOf(theme)?.[key] !== undefined,
    set: (theme, key, value, options) => {
      const hex = normalizeHex(value);
      if (!hex) return;
      // A value equal to the shipped one clears the override rather than storing it,
      // so "I set it back by hand" and "I pressed reset" leave the same state —
      // otherwise the reset control would linger beside a key that is no longer
      // overriding anything.
      if (hex === spec.shipped(theme, key)) {
        reset(theme, key, options);
        return;
      }
      store.update(
        writeTheme(store.read(), theme, { ...entryOf(theme), [key]: hex }),
        options,
      );
    },
    reset,
  };
}

/** Copy the known keys out of an untrusted blob into a clean one: keys `accepts`, values
 *  that normalize to a real hex, nothing else, and no empty themes left behind. This runs
 *  over `localStorage`, whose contents are concatenated into a stylesheet, so it is
 *  load-bearing rather than tidiness. */
export function sanitizeAxis<K extends string>(
  raw: unknown,
  themes: readonly ThemeMode[],
  accepts: (key: string) => key is K,
): AccentOverrides {
  let next: AccentOverrides = {};
  for (const theme of themes) {
    const entry = readTheme(raw, theme);
    if (!entry) continue;
    const kept: Record<string, string> = {};
    for (const [key, value] of Object.entries(entry)) {
      if (!accepts(key) || typeof value !== "string") continue;
      const hex = normalizeHex(value);
      if (hex) kept[key] = hex;
    }
    next = writeTheme(next, theme, kept);
  }
  return next;
}
