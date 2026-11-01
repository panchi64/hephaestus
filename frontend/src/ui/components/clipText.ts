/** `text` cut to at most `limit` characters plus "…", at the last space when one falls
 *  in the final fifth of the window so a word isn't split; `text` itself when it fits. */
export function clipText(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const window = text.slice(0, limit);
  const space = window.lastIndexOf(" ");
  const cut = space >= limit * 0.8 ? window.slice(0, space) : window;
  return cut.trimEnd() + "…";
}
