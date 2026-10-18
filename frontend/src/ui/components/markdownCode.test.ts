import { describe, expect, test } from "bun:test";
import { Marked } from "marked";
import { fenceClosed, markedCode } from "./markdownCode";
import { markedMermaid } from "./markdownMermaid";

/** Configured the way Markdown.tsx configures the shared instance. */
const md = new Marked({ gfm: true, breaks: true }).use(
  markedMermaid,
  ...markedCode,
);
const render = (src: string): string => md.parse(src, { async: false });

const body = (chars: number): string =>
  "const a = 1;\n".repeat(Math.ceil(chars / 13));

describe("fenceClosed", () => {
  test("needs a closing fence at least as long as the opener", () => {
    expect(fenceClosed("```ts\nx\n```")).toBe(true);
    expect(fenceClosed("```ts\nx\n```\n\n")).toBe(true);
    expect(fenceClosed("~~~\nx\n~~~~")).toBe(true);
    expect(fenceClosed("```ts\nx")).toBe(false);
    expect(fenceClosed("```ts")).toBe(false);
    expect(fenceClosed("````\nx\n```")).toBe(false);
    expect(fenceClosed("```\nx\n~~~")).toBe(false);
  });

  test("treats indented code as complete", () => {
    expect(fenceClosed("    x = 1")).toBe(true);
  });
});

describe("markedCode", () => {
  test("highlights a fenced block and keeps its language class", () => {
    const html = render("```ts\nlet a = 1\n```");
    expect(html).toContain('<code class="language-ts">');
    expect(html).toContain('<span class="hljs-keyword">let</span>');
  });

  test("highlights a fence nested in a list", () => {
    expect(render("- step\n\n  ```py\n  def f(): pass\n  ```")).toContain(
      "hljs-keyword",
    );
  });

  test("escapes an unknown-language fence as plain code", () => {
    const html = render("```nope\n<b>&</b>\n```");
    expect(html).toContain("&lt;b&gt;&amp;&lt;/b&gt;");
    expect(html).not.toContain("hljs-");
  });

  test("highlights a short unterminated fence while it streams", () => {
    expect(render("```js\nfunction f(")).toContain("hljs-keyword");
  });

  test("leaves a long unterminated fence plain until it closes", () => {
    const open = "```ts\n" + body(5000);
    expect(render(open)).not.toContain("hljs-");
    expect(render(open + "```")).toContain("hljs-keyword");
  });

  test("leaves mermaid fences to the diagram extension", () => {
    const html = render("```mermaid\ngraph TD; A-->B\n```");
    expect(html).toContain("data-mermaid");
    expect(html).not.toContain("hljs-");
  });
});
