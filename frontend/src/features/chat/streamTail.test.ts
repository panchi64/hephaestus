import { describe, expect, test } from "bun:test";
import { stableStreamSource as stable } from "./streamTail";

describe("complete markdown passes through", () => {
  test.each([
    "plain prose still arriving",
    "Done.\n",
    "# Title\n\nBody text\n",
    "- one\n- two\n",
    "| a | b |\n|---|---|\n| 1 | 2 |\n",
    "Some **bold** and *em* and `code` and ~~gone~~.",
    "a snake_case_name and 2 * 3 * 4 and a*b",
    "```ts\nconst x = 1;\n```\n",
    "$$\nx^2\n$$\n",
    "see [the docs](https://example.com) here",
  ])("%p", (source) => {
    expect(stable(source)).toBe(source);
  });
});

describe("a partial line that could change block type is held", () => {
  test.each([
    ["Intro\n|", "Intro\n"],
    ["Intro\n| a | b", "Intro\n"],
    ["Intro\n-", "Intro\n"],
    ["Intro\n- ", "Intro\n"],
    ["Intro\n*", "Intro\n"],
    ["Intro\n##", "Intro\n"],
    ["Intro\n>", "Intro\n"],
    ["Intro\n12.", "Intro\n"],
    ["Para line\n---", "Para line\n"],
    ["Para line\n==", "Para line\n"],
    ["Intro\n``", "Intro\n"],
    ["Intro\n```py", "Intro\n"],
    ["Intro\n$$", "Intro\n"],
    ["Intro\n<div", "Intro\n"],
  ])("%p", (source, shown) => {
    expect(stable(source)).toBe(shown);
  });

  test.each([
    "Intro\n- item",
    "Intro\n* item",
    "Intro\n## Head",
    "Intro\n> quote",
    "Intro\n12. step",
  ])("streams once the marker has content: %p", (source) => {
    expect(stable(source)).toBe(source);
  });

  test("an ordinary partial line streams", () => {
    expect(stable("Intro\nThe answer is")).toBe("Intro\nThe answer is");
  });

  test("a line opening with bold or a number streams once it is prose", () => {
    expect(stable("Intro\n**Note:** this")).toBe("Intro\n**Note:** this");
    expect(stable("Intro\n2024 was")).toBe("Intro\n2024 was");
    expect(stable("Intro\n#hashtag")).toBe("Intro\n#hashtag");
  });

  test("a bare number may still become a list item", () => {
    expect(stable("Intro\n2024")).toBe("Intro\n");
  });
});

describe("tables", () => {
  test("a header row is held until its delimiter row", () => {
    expect(stable("Intro\n\n| a | b |\n")).toBe("Intro\n\n");
    expect(stable("Intro\n\n| a | b |\n|--")).toBe("Intro\n\n");
  });

  test("once the delimiter lands, only complete rows show", () => {
    const head = "Intro\n\n| a | b |\n|---|---|\n";
    expect(stable(head)).toBe(head);
    expect(stable(`${head}| 1 | 2 |\n| 3`)).toBe(`${head}| 1 | 2 |\n`);
  });

  test("prose with a pipe in it is not a table", () => {
    const source = "Use a | b to pipe\nand then carry on";
    expect(stable(source)).toBe(source);
  });

  test("a table-looking line inside a fence is code", () => {
    const source = "```\n| a | b |\n";
    expect(stable(source)).toBe(source);
  });
});

describe("open blocks", () => {
  test("an open $$ block is held from its opener", () => {
    expect(stable("Before\n$$\nx^2 + ")).toBe("Before\n");
  });

  test("an open fence streams as code", () => {
    expect(stable("```ts\nconst x =")).toBe("```ts\nconst x =");
  });

  test("a closing fence still arriving is held", () => {
    expect(stable("```ts\nconst x = 1;\n``")).toBe("```ts\nconst x = 1;\n");
  });
});

describe("inline styles", () => {
  test("open bold, italic, strike and code close early", () => {
    expect(stable("this is **bold")).toBe("this is **bold**");
    expect(stable("this is *em")).toBe("this is *em*");
    expect(stable("this is _em")).toBe("this is _em_");
    expect(stable("this is ~~gone")).toBe("this is ~~gone~~");
    expect(stable("run `npm i")).toBe("run `npm i`");
  });

  test("nested markers close in reverse order", () => {
    expect(stable("**bold and *both")).toBe("**bold and *both***");
  });

  test("closers go before trailing whitespace", () => {
    expect(stable("this is **bold ")).toBe("this is **bold** ");
  });

  test("a marker still arriving is held", () => {
    expect(stable("this is *")).toBe("this is ");
    expect(stable("this is **bold*")).toBe("this is **bold**");
  });

  test("markers inside code spans are ignored", () => {
    expect(stable("use `a*b` and `**`")).toBe("use `a*b` and `**`");
  });

  test("escaped markers are literal", () => {
    expect(stable("a \\*star")).toBe("a \\*star");
  });

  test("emphasis opened earlier in the paragraph closes", () => {
    expect(stable("start **bold\nstill bold")).toBe(
      "start **bold\nstill bold**",
    );
  });

  test("emphasis in an earlier paragraph does not", () => {
    expect(stable("a **b** c\n\nnext *x")).toBe("a **b** c\n\nnext *x*");
  });

  test("a closer is never put at the start of a line", () => {
    expect(stable("start **bold\n")).toBe("start **bold**\n");
  });

  test("a single marker left open on a finished line stays literal", () => {
    expect(stable("call *args here\nand more")).toBe(
      "call *args here\nand more",
    );
  });
});

describe("tables", () => {
  test("under a delimiter row, a partial row without pipes is held", () => {
    expect(stable("a | b\n--- | ---\n1 | 2\n3 |")).toBe(
      "a | b\n--- | ---\n1 | 2\n",
    );
    expect(stable("a | b\n--- | ---\nplain")).toBe("a | b\n--- | ---\n");
  });
});

describe("links", () => {
  test("an unclosed link streams its text as prose", () => {
    expect(stable("see [the do")).toBe("see the do");
    expect(stable("see [the docs]")).toBe("see the docs");
    expect(stable("see [the docs](https://exa")).toBe("see the docs");
  });

  test("a bracket inside a code span is not a link", () => {
    expect(stable("index `a[0")).toBe("index `a[0`");
  });

  test("a bracket after a word is an index, not a link", () => {
    expect(stable("index into arr[0]")).toBe("index into arr[0]");
    expect(stable("index into arr[0")).toBe("index into arr[0");
  });

  test("a task box still being typed is held", () => {
    expect(stable("Intro\n- [ ]")).toBe("Intro\n");
    expect(stable("Intro\n- [x")).toBe("Intro\n");
    expect(stable("Intro\n- [ ] ta")).toBe("Intro\n- [ ] ta");
  });
});

test("CRLF line endings", () => {
  expect(stable("Intro\r\n- ")).toBe("Intro\r\n");
  expect(stable("Intro\r\n\r\n| a | b |\r\n")).toBe("Intro\r\n\r\n");
});
