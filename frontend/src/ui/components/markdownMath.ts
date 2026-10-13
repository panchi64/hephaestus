import katex from "katex";
import type { MarkedExtension, TokenizerAndRendererExtension } from "marked";

/**
 * LaTeX/math support for the `marked` pipeline (see Markdown.tsx).
 *
 * Two delimiter families, both common in LLM output, are recognised:
 *   • display (block): `$$ … $$` and `\[ … \]`
 *   • inline:          `$ … $`  and `\( … \)`
 *
 * Math is tokenised at the lexer level, so it never fires inside code spans or
 * fenced blocks — those are claimed by `marked`'s own (higher-priority) code
 * tokenizers first, leaving e.g. `` `$x$` `` and ```` ```$$…$$``` ```` untouched.
 *
 * KaTeX renders with `throwOnError: false`, so malformed or half-streamed math
 * degrades to an inline error string rather than throwing — incomplete math mid
 * stream simply stays raw text until its closing delimiter arrives.
 */

const render = (tex: string, displayMode: boolean): string =>
  katex.renderToString(tex, {
    displayMode,
    throwOnError: false,
    output: "html",
  });

const blockMath: TokenizerAndRendererExtension = {
  name: "blockMath",
  level: "block",
  start(src) {
    const m = src.match(/\$\$|\\\[/);
    return m?.index;
  },
  tokenizer(src) {
    const m =
      /^\$\$([\s\S]+?)\$\$/.exec(src) ?? /^\\\[([\s\S]+?)\\\]/.exec(src);
    if (!m) return undefined;
    const text = m[1].trim();
    if (!text) return undefined;
    return { type: "blockMath", raw: m[0], text };
  },
  renderer(token) {
    return `<div class="heph-math-block">${render(token.text, true)}</div>`;
  },
};

const inlineMath: TokenizerAndRendererExtension = {
  name: "inlineMath",
  level: "inline",
  start(src) {
    const m = src.match(/\$|\\\(/);
    return m?.index;
  },
  tokenizer(src) {
    // `\( … \)` — explicit, unambiguous, no heuristics needed.
    const paren = /^\\\(([\s\S]+?)\\\)/.exec(src);
    if (paren) {
      const text = paren[1].trim();
      if (!text) return undefined;
      return { type: "inlineMath", raw: paren[0], text };
    }
    // `$ … $` — guarded against currency ("$5 and $6"): the opening `$` must not
    // be followed by whitespace, the content must not end in whitespace, and the
    // closing `$` must not be followed by a digit.
    const dollar = /^\$(?![\s$])((?:\\.|[^\n$])*?[^\s\\])\$(?!\d)/.exec(src);
    if (dollar) {
      return { type: "inlineMath", raw: dollar[0], text: dollar[1] };
    }
    return undefined;
  },
  renderer(token) {
    return render(token.text, false);
  },
};

/** Pass to `marked.use(...)` to enable `$`/`$$`/`\(`/`\[` math via KaTeX. */
export const markedMath: MarkedExtension = {
  extensions: [blockMath, inlineMath],
};
