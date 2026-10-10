/** The keyboard rules of `ChoiceList`, kept pure so they can be tested without a DOM. */

/** Number keys 1–9 address the first nine rows; past that the arrows still reach. */
export const NUMBER_KEYS = 9;

/** The row a number key picks, or `null` when the key addresses no row. */
export function numberKeyIndex(key: string, count: number): number | null {
  if (!/^[1-9]$/.test(key)) return null;
  const k = Number(key);
  return k <= Math.min(count, NUMBER_KEYS) ? k - 1 : null;
}

/** The row ↑/↓ moves the pick to — wrapping at both ends, as a radiogroup does. */
export function stepIndex(index: number, dir: 1 | -1, count: number): number {
  if (count <= 0) return -1;
  return (index + dir + count) % count;
}

/** The footer's key hints: what the keys do here, and nothing they don't. */
export function choiceHints(count: number, freeText: boolean): string {
  const n = Math.min(count, NUMBER_KEYS);
  const parts: string[] = [];
  if (n > 0) parts.push(n === 1 ? "1 pick" : `1–${n} pick`);
  if (count > 1) parts.push("↑↓ move");
  if (freeText) parts.push("type to answer freely");
  return parts.join(" · ");
}
