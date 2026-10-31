import { describe, expect, test } from "bun:test";
import { markdownPreview } from "./markdownPreview";

describe("markdownPreview", () => {
  test("strips a heading marker", () => {
    expect(markdownPreview("## Findings\n\nBody")).toBe("Findings");
  });

  test("strips emphasis, code and links", () => {
    expect(
      markdownPreview("**Summary:** see `app.py` and [the docs](http://x)"),
    ).toBe("Summary: see app.py and the docs");
  });

  test("skips rules and fences to the first line with text", () => {
    expect(markdownPreview("---\n```\nconst a = 1;")).toBe("const a = 1;");
  });

  test("keeps the underscores of a snake_case word", () => {
    expect(markdownPreview("- renamed foo_bar_baz")).toBe(
      "renamed foo_bar_baz",
    );
  });

  test("is undefined for blank text", () => {
    expect(markdownPreview("\n  \n")).toBeUndefined();
  });
});
