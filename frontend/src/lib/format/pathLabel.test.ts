import { describe, expect, test } from "bun:test";
import { pathLabel } from "./index";

describe("pathLabel", () => {
  test("names a directory by its last two segments", () => {
    expect(pathLabel("/Users/me/code/acme/frontend")).toEqual({
      parent: "acme",
      name: "frontend",
    });
  });

  test("stops at two segments however deep the path", () => {
    // The rail is permanently on screen. A full path across it spells out the
    // operator's clients to anyone standing behind them, so the depth is a cap and
    // not a consequence of the input.
    expect(pathLabel("/a/b/c/d/e/f/g")).toEqual({ parent: "f", name: "g" });
  });

  test("a top-level directory has no parent", () => {
    expect(pathLabel("/work")).toEqual({ parent: "", name: "work" });
  });

  test("a trailing separator is not a segment", () => {
    // Otherwise the name would come back empty and the heading would be blank.
    expect(pathLabel("/Users/me/hephaestus/")).toEqual({
      parent: "me",
      name: "hephaestus",
    });
  });

  test("repeated separators collapse", () => {
    expect(pathLabel("/Users//me///hephaestus")).toEqual({
      parent: "me",
      name: "hephaestus",
    });
  });

  test("Windows separators read the same way", () => {
    // The path is the host's and this runs in a browser, which knows nothing about
    // which host produced it.
    expect(pathLabel("C:\\Users\\me\\code\\hephaestus")).toEqual({
      parent: "code",
      name: "hephaestus",
    });
  });

  test("a path with no separator at all is its own name", () => {
    expect(pathLabel("hephaestus")).toEqual({ parent: "", name: "hephaestus" });
  });

  test("an empty path degrades to itself rather than to undefined", () => {
    expect(pathLabel("")).toEqual({ parent: "", name: "" });
  });
});
