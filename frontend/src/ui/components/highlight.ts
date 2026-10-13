/** Thin, lazy syntax-highlighting wrapper over Shiki — no WASM (the pure-JS regex
 *  engine, `shiki/engine/javascript`), and only the language grammars actually
 *  requested load, on demand. A single highlighter instance and its loaded
 *  languages are cached at module scope for the lifetime of the tab.
 *
 *  Colors are never hardcoded: the one custom theme below carries CSS `var()`
 *  strings that resolve the app's own tokens (`theme/tokens.css`), so highlighted
 *  code re-colors with the live Phosphor/Paper toggle exactly like everything
 *  else — and stays monochrome (base/dim/bright), since accent colors are
 *  reserved for diffs (`src/ui/CLAUDE.md`), not decorative token coloring.
 *
 *  `highlightToHtml` never throws: any failure (unknown language, a grammar that
 *  fails to load) resolves `null` so callers just keep their plain, unhighlighted
 *  rendering. */

import {
  createHighlighterCore,
  type HighlighterCore,
  type LanguageRegistration,
  type ThemeRegistrationRaw,
} from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

/** The one theme this app ever highlights with: default text, dim comments,
 *  bright keywords/entities — the same three tones `Text`'s `tone` prop uses
 *  elsewhere, referencing the live CSS custom properties directly rather than
 *  a snapshotted hex value. No `colors` block — only the token spans matter,
 *  since the surrounding `<pre>`/gutter chrome is `CodeBlock`'s, not Shiki's. */
const THEME_NAME = "hephaestus-mono";
const HEPHAESTUS_THEME: ThemeRegistrationRaw = {
  name: THEME_NAME,
  type: "dark",
  settings: [
    {
      settings: { foreground: "var(--text)" },
    },
    {
      scope: ["comment", "punctuation.definition.comment"],
      settings: { foreground: "var(--text-dim)" },
    },
    {
      scope: [
        "keyword",
        "storage",
        "storage.type",
        "storage.modifier",
        "keyword.control",
        "keyword.operator.new",
        "constant.language",
        "variable.language",
        "entity.name.tag",
        "entity.name.function",
        "entity.name.class",
        "support.type",
        "support.class",
      ],
      settings: { foreground: "var(--text-bright)" },
    },
  ],
};

/** Alias -> canonical Shiki language id + its dynamic loader. Each loader is a
 *  literal `import()` (not a computed path) so the bundler can code-split every
 *  grammar into its own chunk. */
const LANG_LOADERS: Record<
  string,
  () => Promise<{ default: LanguageRegistration[] }>
> = {
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  bash: () => import("shiki/langs/bash.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
};

/** Caller-facing aliases (extensions, common short names) -> the canonical id
 *  used as both the `LANG_LOADERS` key and Shiki's own grammar name. */
const LANG_ALIASES: Record<string, keyof typeof LANG_LOADERS> = {
  ts: "typescript",
  typescript: "typescript",
  tsx: "tsx",
  js: "javascript",
  javascript: "javascript",
  jsx: "jsx",
  py: "python",
  python: "python",
  rs: "rust",
  rust: "rust",
  go: "go",
  json: "json",
  css: "css",
  html: "html",
  sh: "bash",
  bash: "bash",
  shell: "bash",
  md: "markdown",
  markdown: "markdown",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  sql: "sql",
  diff: "diff",
};

let highlighterPromise: Promise<HighlighterCore> | null = null;
const loadedLangs = new Set<string>();

function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      themes: [HEPHAESTUS_THEME],
      langs: [],
      engine: createJavaScriptRegexEngine(),
    });
  }
  return highlighterPromise;
}

/** Loads `canonicalId`'s grammar into the shared highlighter, once. */
async function ensureLanguage(
  highlighter: HighlighterCore,
  canonicalId: keyof typeof LANG_LOADERS,
): Promise<void> {
  if (loadedLangs.has(canonicalId)) return;
  const mod = await LANG_LOADERS[canonicalId]();
  await highlighter.loadLanguage(mod.default);
  loadedLangs.add(canonicalId);
}

/** Highlights `code` as `lang` (an alias from the map above, case-insensitive)
 *  to an HTML string (Shiki's usual `<pre class="shiki">…</pre>`, token spans
 *  colored via the `var()` theme above). Resolves `null` — never throws — on an
 *  unrecognized language or any load/highlight failure, so callers always have
 *  a safe plain-text fallback to keep rendering. */
export async function highlightToHtml(
  code: string,
  lang: string,
): Promise<string | null> {
  const canonicalId = LANG_ALIASES[lang.toLowerCase()];
  if (!canonicalId) return null;
  try {
    const highlighter = await getHighlighter();
    await ensureLanguage(highlighter, canonicalId);
    return highlighter.codeToHtml(code, {
      lang: canonicalId,
      theme: THEME_NAME,
    });
  } catch {
    return null;
  }
}
