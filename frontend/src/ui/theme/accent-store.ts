import { ACCENT_DEFAULTS, type AccentToken } from "./accents";
import { createAccentAxis } from "./accent-axis";
import { accentOverrides, update } from "./accent-overrides";

/**
 * The five accent **meanings**, read and written per theme mode.
 *
 * The storage, the guard and the stylesheet are `accent-overrides.ts`; the pruning and
 * clear-on-default rules are `accent-axis.ts`. What is left here is the vocabulary —
 * which keys are held and what they fall back to.
 */
const axis = createAccentAxis<AccentToken>(
  { shipped: (theme, token) => ACCENT_DEFAULTS[theme][token] },
  { read: accentOverrides, update },
);

/** The value a token currently resolves to for a theme — the override if there
 *  is one, otherwise the shipped default. */
export const accentValue = axis.value;

/** Whether this token carries an override for this theme. */
export const isAccentOverridden = axis.isOverridden;

/** Set one token for one theme. A value equal to the shipped default clears the
 *  override rather than storing it. */
export const setAccent = axis.set;

/** Clear one token's override, back to the shipped value. */
export const resetAccent = axis.reset;
