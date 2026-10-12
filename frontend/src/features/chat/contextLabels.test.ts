import { describe, expect, test } from "bun:test";
import { segmentLabel } from "./contextLabels";

describe("a slug becomes a label without a registry to maintain", () => {
  test("an unknown id reads as its own words", () => {
    // Every tool category and instruction provider the backend grows later has to
    // render correctly with no edit here — otherwise this list becomes a second
    // catalog to keep in step with the real one.
    expect(segmentLabel("files")).toBe("Files");
    expect(segmentLabel("tool_results")).toBe("Tool results");
    expect(segmentLabel("corpus_snippets")).toBe("Corpus snippets");
  });

  test("the few slugs whose own words would mislead are named", () => {
    expect(segmentLabel("base")).toBe("Base prompt");
    expect(segmentLabel("external")).toBe("MCP & connectors");
    expect(segmentLabel("repo")).toBe("Project instructions");
  });

  test("the thread's two axes read as a pair, and an adjective becomes a thing", () => {
    // "Workspace" and "Level" side by side in the breakdown name a posture and a bare
    // scale; the second one has to say what it is a level of.
    expect(segmentLabel("workspace_posture")).toBe("Workspace posture");
    expect(segmentLabel("level")).toBe("Permission posture");
    // The block is an index of tool groups, not a state — "Dormant" alone labels a
    // row with an adjective and leaves the operator to supply the noun.
    expect(segmentLabel("dormant")).toBe("Dormant tools");
  });

  test("a slug with no words left in it still says something", () => {
    // The label and the tool table's fallback are one rule now (`sentenceCase`), and
    // this is the case that proved they weren't: the copy here upper-cased position 0
    // of an empty string and rendered the row blank.
    expect(segmentLabel("_")).toBe("_");
    expect(segmentLabel("  ")).toBe("  ");
  });

  test("the gauge's row and the work log's injection row are one word", () => {
    // Both surfaces name a contributor through this function, so the operator never has
    // to work out that "Skills · ~4k" in the popover and the injected block on the rail
    // are the same block seen from two distances.
    expect(segmentLabel("skill_catalog")).toBe("Skills");
    expect(segmentLabel("tasks")).toBe("Task list");
  });
});
