import { describe, expect, it } from "bun:test";
import {
  buildFileTree,
  charCount,
  composeMarkdownFile,
  compareByName,
  compareByUpdated,
  filterByTab,
  frontmatterLineCount,
  isMarkdownPath,
  isTextPath,
  searchText,
  stripFrontmatter,
} from "./model";

const items = [
  {
    name: "b",
    description: "Second one",
    published: true,
    updatedAt: "2026-01-02",
  },
  {
    name: "a",
    description: "First",
    published: false,
    updatedAt: "2026-01-03",
  },
  { name: "c", description: "Third", published: true, updatedAt: "2026-01-01" },
];

describe("filterByTab", () => {
  it("keeps everything on all, and partitions published from draft", () => {
    expect(filterByTab(items, "all")).toHaveLength(3);
    expect(filterByTab(items, "published").map((i) => i.name)).toEqual([
      "b",
      "c",
    ]);
    expect(filterByTab(items, "draft").map((i) => i.name)).toEqual(["a"]);
  });
});

describe("search and sort", () => {
  it("searches name and description together", () => {
    expect(searchText(items[0])).toBe("b Second one");
  });
  it("orders by name and by update time", () => {
    expect([...items].sort(compareByName).map((i) => i.name)).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect([...items].sort(compareByUpdated).map((i) => i.name)).toEqual([
      "c",
      "b",
      "a",
    ]);
  });
});

describe("charCount", () => {
  it("shows the limit only when there is one", () => {
    expect(charCount("abc", 64)).toBe("3 / 64");
    expect(charCount("abc")).toBe("3");
  });
});

describe("buildFileTree", () => {
  it("lists the main file first, then folders, then loose files", () => {
    const tree = buildFileTree("SKILL.md", [
      "notes.md",
      "references/examples.md",
      "references/api.md",
    ]);
    expect(tree.map((n) => n.name)).toEqual([
      "SKILL.md",
      "references",
      "notes.md",
    ]);
    const dir = tree[1];
    expect(dir.kind === "dir" && dir.children.map((c) => c.path)).toEqual([
      "references/api.md",
      "references/examples.md",
    ]);
  });
  it("is just the main file for a bundle with nothing else", () => {
    expect(buildFileTree("a.md", [])).toEqual([
      { kind: "file", name: "a.md", path: "a.md" },
    ]);
  });
});

describe("markdown files", () => {
  it("recognises markdown paths", () => {
    expect(isMarkdownPath("references/x.MD")).toBe(true);
    expect(isMarkdownPath("script.py")).toBe(false);
  });
  it("composes frontmatter, skipping empty keys, and strips it again", () => {
    const text = composeMarkdownFile(
      [
        ["name", "demo"],
        ["description", "Does: things"],
        ["license", null],
        ["allowed-tools", ["read", "grep"]],
      ],
      "Body",
    );
    expect(text).toBe(
      '---\nname: demo\ndescription: "Does: things"\nallowed-tools: ["read","grep"]\n---\n\nBody',
    );
    expect(stripFrontmatter(text)).toBe("Body");
    expect(stripFrontmatter("no matter")).toBe("no matter");
  });
});

describe("frontmatterLineCount", () => {
  it("counts the fenced block, fences included", () => {
    expect(frontmatterLineCount("---\nname: a\n---\nbody")).toBe(3);
  });
  it("is zero without an opening or closing fence", () => {
    expect(frontmatterLineCount("body\n---\nx\n---")).toBe(0);
    expect(frontmatterLineCount("---\nname: a\nbody")).toBe(0);
  });
});

describe("isTextPath", () => {
  it("accepts text files and rejects binaries", () => {
    expect(isTextPath("references/a.md")).toBe(true);
    expect(isTextPath("scripts/run.py")).toBe(true);
    expect(isTextPath("logo.png")).toBe(false);
  });
});
