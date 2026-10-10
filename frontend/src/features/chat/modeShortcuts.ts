/**
 * The mode switch's keys: Control+Shift+1–3 on every platform.
 *
 * ⌘1–3 and Ctrl+1–3 are the browser's own tab switching, so the chord adds Shift. It is
 * matched on the physical key (`code:digit1`) because Shift changes what `event.key`
 * reads. On macOS that is ⌃⇧1, which neither Chrome nor the system claims.
 */

/** The keymap combo for the mode at `index` (0-based, in rail order). */
export function modeShortcutCombo(index: number): string {
  return `ctrl+shift+code:digit${index + 1}`;
}

/** The chord as the tooltip prints it. */
export function modeShortcutLabel(index: number, mac: boolean): string {
  return mac ? `⌃⇧${index + 1}` : `Ctrl+Shift+${index + 1}`;
}
