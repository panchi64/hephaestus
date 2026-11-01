import { describe, expect, test } from "bun:test";
import { highlightCode, splitHighlightedLines } from "./codeHighlight";

/** What the copy button reads: the DOM's text content of the highlighted HTML. */
function textOf(html: string): string {
  return html
    .replace(/<[^>]*>/g, "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&");
}

describe("highlightCode", () => {
  test("highlights a known language", () => {
    const html = highlightCode("const x = 1; // note", "typescript");
    expect(html).toContain('<span class="hljs-keyword">const</span>');
    expect(html).toContain('<span class="hljs-comment">// note</span>');
  });

  test("resolves the aliases fences and file extensions use, in any case", () => {
    const aliases = [
      "ts",
      "tsx",
      "js",
      "jsx",
      "py",
      "rs",
      "go",
      "json",
      "css",
      "html",
      "sh",
      "bash",
      "zsh",
      "shell",
      "console",
      "md",
      "yml",
      "toml",
      "sql",
      "diff",
      "c",
      "c++",
      "cpp",
      "c#",
      "java",
      "kt",
      "swift",
      "rb",
      "php",
      "dockerfile",
      "xml",
      "graphql",
      "lua",
      "ini",
      "TS",
      "Python",
    ];
    for (const lang of aliases)
      expect({ lang, ok: highlightCode("a", lang) !== null }).toEqual({
        lang,
        ok: true,
      });
  });

  test("returns null for no, unknown or oversize input", () => {
    expect(highlightCode("x", "")).toBeNull();
    expect(highlightCode("x", "no-such-language")).toBeNull();
    expect(highlightCode("const a = 1;\n".repeat(21_000), "ts")).toBeNull();
    expect(highlightCode("x".repeat(1000), "ts")).not.toBeNull();
    expect(highlightCode("x".repeat(1001), "ts")).toBeNull();
    expect(highlightCode("ok\n" + "x".repeat(40_000), "ts")).toBeNull();
  });

  test("escapes markup and keeps the exact source text", () => {
    const source = "const s = \"<script>alert(1)</script>\" && 'a';\n\n";
    const html = highlightCode(source, "ts") ?? "";
    expect(html).not.toContain("<script>");
    expect(textOf(html)).toBe(source);
  });
});

describe("splitHighlightedLines", () => {
  test("balances spans that cross a newline on every line", () => {
    const source = "/* one\n   two */\nconst s = `a\nb`;";
    const lines = splitHighlightedLines(highlightCode(source, "ts") ?? "");
    expect(lines).toHaveLength(4);
    for (const line of lines)
      expect(line.match(/<span/g)?.length ?? 0).toBe(
        line.match(/<\/span>/g)?.length ?? 0,
      );
    expect(lines[1]).toStartWith('<span class="hljs-comment">');
    expect(lines.map(textOf).join("\n")).toBe(source);
  });

  test("keeps empty and trailing lines", () => {
    expect(splitHighlightedLines("a\n\nb\n")).toEqual(["a", "", "b", ""]);
    expect(splitHighlightedLines("")).toEqual([""]);
  });

  test("gives an empty line inside a span no markup", () => {
    const lines = splitHighlightedLines(
      highlightCode("/* a\n\nb */", "ts") ?? "",
    );
    expect(lines[1]).toBe("");
  });
});
