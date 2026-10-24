/** Synchronous syntax highlighting over highlight.js's common language set. Output is
 *  escaped source in class-only `hljs-*` spans, coloured by `theme/styles/code-tokens.css`. */

import hljs from "highlight.js/lib/common";
import dockerfile from "highlight.js/lib/languages/dockerfile";

hljs.registerLanguage("dockerfile", dockerfile);

/** Above this, a block renders plain. Highlighting runs on the main thread at a measured
 *  worst case of ~1.2 ms per KB, so one call stays under ~300 ms. */
const MAX_HIGHLIGHT_CHARS = 262_144;
/** A line longer than this also renders the block plain: highlight.js is quadratic in
 *  the length of one unbroken token (a 40,000-character identifier takes ~2 s). */
const MAX_LINE_CHARS = 1000;
const LONG_LINE = new RegExp(`[^\\n]{${MAX_LINE_CHARS + 1}}`);

/** `code` highlighted as `lang` (a name or alias highlight.js knows, any case), as
 *  HTML. `null` for no language, an unknown one, an oversize block or line, or any
 *  highlighter failure, so callers keep their plain rendering. */
export function highlightCode(code: string, lang: string): string | null {
  const language = lang.toLowerCase();
  if (
    code.length > MAX_HIGHLIGHT_CHARS ||
    LONG_LINE.test(code) ||
    !hljs.getLanguage(language)
  )
    return null;
  try {
    return hljs.highlight(code, { language, ignoreIllegals: true }).value;
  } catch {
    return null;
  }
}

const SPAN_TAG = /<span[^>]*>|<\/span>/g;

/** Splits `highlightCode` output into one balanced HTML string per source line. A span
 *  that crosses a newline (a block comment, a multi-line string) is closed at the end
 *  of each line and reopened at the start of the next, so every row keeps its colour.
 *  A line with no text is `""`, whatever spans it sits inside. */
export function splitHighlightedLines(html: string): string[] {
  const lines: string[] = [];
  const open: string[] = [];
  let line = "";
  let hasText = false;
  const endLine = (): void => {
    lines.push(hasText ? line + "</span>".repeat(open.length) : "");
  };
  const addText = (text: string): void => {
    const parts = text.split("\n");
    line += parts[0];
    hasText ||= parts[0] !== "";
    for (const part of parts.slice(1)) {
      endLine();
      line = open.join("") + part;
      hasText = part !== "";
    }
  };
  let last = 0;
  for (const match of html.matchAll(SPAN_TAG)) {
    addText(html.slice(last, match.index));
    line += match[0];
    if (match[0] === "</span>") open.pop();
    else open.push(match[0]);
    last = match.index + match[0].length;
  }
  addText(html.slice(last));
  endLine();
  return lines;
}
