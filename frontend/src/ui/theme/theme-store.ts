import { createSignal } from "solid-js";
import { isServer } from "solid-js/web";

/**
 * Central theme store. Theme is UI-owned state, so it lives here in `ui/theme`
 * rather than in `lib/stores`. The user's *preference* is one of three modes;
 * the *resolved* palette (what actually drives the cascade) is written to
 * `document.documentElement.dataset.theme`, which every color token in
 * tokens.css reads. "system" resolves against `prefers-color-scheme` and is
 * re-resolved on OS changes by ThemeProvider.
 */
export type ThemeMode = "phosphor" | "paper";
export type ThemePreference = ThemeMode | "system";

export const THEME_STORAGE_KEY = "hephaestus:theme";
export const DEFAULT_THEME: ThemeMode = "phosphor";
export const DEFAULT_PREFERENCE: ThemePreference = "phosphor";

/** The order the toggle cycles through on each click. */
export const THEME_CYCLE: readonly ThemePreference[] = [
  "phosphor",
  "paper",
  "system",
];

function readStored(): ThemePreference {
  if (isServer || typeof localStorage === "undefined")
    return DEFAULT_PREFERENCE;
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "phosphor" || stored === "paper" || stored === "system"
    ? stored
    : DEFAULT_PREFERENCE;
}

/** The palette the OS is currently asking for (imperative one-shot read). */
export function systemTheme(): ThemeMode {
  if (isServer || typeof matchMedia === "undefined") return DEFAULT_THEME;
  return matchMedia("(prefers-color-scheme: light)").matches
    ? "paper"
    : "phosphor";
}

// Reactive mirror of the OS palette so `resolveTheme` (and anything derived
// from it) re-runs when the OS flips. ThemeProvider keeps it current via
// syncSystemTheme() on the prefers-color-scheme change event.
const [systemMode, setSystemMode] = createSignal<ThemeMode>(systemTheme());

/** Re-read the OS palette into the reactive mirror; call on a media change. */
export function syncSystemTheme(): void {
  setSystemMode(systemTheme());
}

/** Collapse a preference to the concrete palette to apply. */
export function resolveTheme(pref: ThemePreference): ThemeMode {
  return pref === "system" ? systemMode() : pref;
}

const [preference, setPreferenceSignal] =
  createSignal<ThemePreference>(readStored());

/** Reflect the resolved palette onto <html data-theme>; the cascade truth. */
export function applyTheme(pref: ThemePreference): void {
  if (isServer) return;
  document.documentElement.dataset.theme = resolveTheme(pref);
}

export function setTheme(pref: ThemePreference): void {
  setPreferenceSignal(pref);
  if (!isServer && typeof localStorage !== "undefined") {
    localStorage.setItem(THEME_STORAGE_KEY, pref);
  }
  applyTheme(pref);
}

/** Step to the next preference in the cycle. */
export function toggleTheme(): void {
  const next =
    THEME_CYCLE[(THEME_CYCLE.indexOf(preference()) + 1) % THEME_CYCLE.length];
  setTheme(next);
}

export { preference };
