import type { MarkedExtension } from "marked";
import { markedHighlight } from "marked-highlight";
import { highlightCode } from "./codeHighlight";

/** An open fence is re-highlighted in full on every streaming token, so past this size
 *  it stays plain until its closing fence arrives: ~5 ms at the measured worst case. */
const OPEN_FENCE_MAX_CHARS = 4096;

const FENCE_OPENER = /^ {0,3}(`{3,}|~{3,})/;

/** Whether a code token's source ends in its closing fence. Indented code has no
 *  fence and is complete as lexed. */
export function fenceClosed(raw: string): boolean {
  const opener = FENCE_OPENER.exec(raw)?.[1];
  if (!opener) return true;
  const lines = raw.trimEnd().split("\n");
  const last = lines[lines.length - 1].trim();
  return (
    lines.length > 1 &&
    last.length >= opener.length &&
    [...last].every((ch) => ch === opener[0])
  );
}

const openFenceGuard: MarkedExtension = {
  walkTokens(token) {
    if (
      token.type === "code" &&
      token.text.length > OPEN_FENCE_MAX_CHARS &&
      !fenceClosed(token.raw)
    )
      token.lang = "";
  },
};

/** Syntax-highlighted fences for the `marked` pipeline (see Markdown.tsx). Returning the
 *  source unchanged makes `marked-highlight` escape it as plain code. */
export const markedCode: MarkedExtension[] = [
  markedHighlight({
    highlight: (code, lang) => highlightCode(code, lang) ?? code,
  }),
  // After the highlighter, because marked runs the most recently registered
  // `walkTokens` first.
  openFenceGuard,
];
