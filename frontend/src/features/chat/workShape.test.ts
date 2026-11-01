import { describe, expect, test } from "bun:test";
import { latestReason, workShape } from "./workShape";
import { groupBlocks } from "./blocks";
import type { AssistantBlock, HostCommandPhase, ToolStatus } from "./model";

function tool(
  id: string,
  name: string,
  detail?: string,
  status: ToolStatus = "ok",
  narration?: string,
): AssistantBlock {
  return {
    kind: "tool",
    id,
    tool: { id, name, args: "", status, detail, narration },
  };
}

function narrated(id: string, narration: string, detail?: string) {
  return tool(id, "files_read_file", detail, "ok", narration);
}

function think(id: string): AssistantBlock {
  return { kind: "thinking", id, text: "considering" };
}

function host(id: string, phase: HostCommandPhase): AssistantBlock {
  return {
    kind: "host_command",
    id,
    command: {
      toolCallId: id,
      name: "shell_run_command",
      command: "ls -la",
      phase,
    },
  };
}

/** The shape of a set of blocks, as the caller sees it — through `groupBlocks`,
 *  the same way the work log builds the groups it hands over. */
function shape(blocks: AssistantBlock[]) {
  return workShape(groupBlocks(blocks));
}

function reason(blocks: AssistantBlock[]) {
  return latestReason(groupBlocks(blocks));
}

describe("workShape reports the run's last step", () => {
  test("the latest call wins, not the first or the most frequent", () => {
    // Three reads then one search: a tally would have led with "Read ×3", which
    // is exactly the reading this replaced.
    const s = shape([
      tool("a", "files_read_file", "src/a.ts"),
      tool("b", "files_read_file", "src/b.ts"),
      tool("c", "files_read_file", "src/c.ts"),
      tool("d", "web_search", "pydantic ai streaming"),
    ]);
    expect(s.latest).toEqual({
      icon: "search",
      label: "Web search",
      detail: "pydantic ai streaming",
    });
  });

  test("the step keeps the tool's own glyph and label, not its family's", () => {
    // `files_search_files` and `files_read_file` share the `files` category but
    // are different acts — the registry table exists so the header says "Search
    // files" rather than bucketing both as "File".
    expect(shape([tool("a", "files_search_files")]).latest).toMatchObject({
      icon: "search",
      label: "Search files",
    });
    expect(shape([tool("a", "files_read_file")]).latest).toMatchObject({
      icon: "file",
      label: "Read",
    });
  });

  test("a call with no salient argument still names its kind", () => {
    // `detail` is undefined whenever `toolSummary` found nothing worth lifting.
    // The header must degrade to the bare verb rather than to nothing.
    const s = shape([tool("a", "memory_recall")]);
    expect(s.latest?.label).toBe("Recall");
    expect(s.latest?.detail).toBeUndefined();
  });

  test("an unregistered tool still gets a family glyph and a humanized label", () => {
    // `external_*` connector tools are discovered per operator and can never be
    // enumerated in the table, so the header must not depend on a table hit.
    expect(shape([tool("a", "external_linear_create_issue")]).latest).toEqual({
      icon: "plug",
      label: "Linear create issue",
      detail: undefined,
    });
  });

  test("reasoning is a step like any other when it is the last one", () => {
    const s = shape([tool("a", "web_search", "q"), think("b")]);
    expect(s.latest).toEqual({ icon: "cpu", label: "Reasoning" });
  });

  test("a host command reads as its command line", () => {
    expect(shape([host("a", "ok")]).latest).toEqual({
      icon: "terminal",
      label: "Host command",
      detail: "ls -la",
    });
  });

  test("a kind with no step of its own is scanned past, not led with", () => {
    // A View chip is the live case: it stopped folding when the run floor dropped to
    // one (`isCollapsible`), so it has no arm in `stepOf` any more. The scan must
    // walk back to the last step that *is* work rather than summarizing as nothing —
    // which is the whole reason the `default` arm returns undefined instead of a
    // placeholder.
    const s = shape([
      tool("a", "files_read_file", "agent.py"),
      { kind: "view_version", id: "v", snapshotId: "s1", title: "Chart" },
    ]);
    expect(s.latest).toEqual({
      icon: "file",
      label: "Read",
      detail: "agent.py",
    });
  });
});

describe("a fold made only of injected context still names itself", () => {
  test("the header says which block, and that it was put there", () => {
    // A turn whose whole preamble folds must not summarize as nothing — and the word
    // "injected" is what keeps the fold's own header from reading like work the model
    // chose to do.
    const s = shape([
      {
        kind: "context",
        id: "c1",
        injection: {
          contributor: "skill_catalog",
          placement: "instructions",
          tokens: 900,
          text: "…",
          truncated: false,
        },
      },
    ]);
    expect(s.latest).toEqual({
      icon: "inject",
      label: "Skills",
      detail: "injected",
    });
  });
});

describe("the header's reason and the row beneath it", () => {
  test("it is the latest narration, and the step keeps its argument", () => {
    // The header leads with the reason; the indented row under it names the act.
    // Repeating the narration in the row would say the same sentence twice.
    const blocks = [
      narrated("a", "Checking the old loop", "old.py"),
      narrated("b", "Finding where cancel is handled", "engine.py"),
    ];
    expect(reason(blocks)).toBe("Finding where cancel is handled");
    expect(shape(blocks).latest).toEqual({
      icon: "file",
      label: "Read",
      detail: "engine.py",
    });
  });

  test("a trailing step with no reason keeps the last stated one", () => {
    // Thinking between calls must not blank the header: the intent the model last
    // stated is still the one it is acting on.
    const blocks = [
      narrated("a", "Checking the old loop", "old.py"),
      think("b"),
    ];
    expect(reason(blocks)).toBe("Checking the old loop");
    expect(shape(blocks).latest?.label).toBe("Reasoning");
  });

  test("a host command's explanation is its reason", () => {
    const block = host("a", "ok");
    if (block.kind === "host_command")
      block.command.explanation = "Listing the tree";
    expect(reason([block])).toBe("Listing the tree");
  });

  test("a run nobody explained has no reason rather than a made-up one", () => {
    expect(reason([tool("a", "web_search", "q")])).toBeUndefined();
  });

  test("latestId follows the newest step, so its row can replay its arrival", () => {
    expect(
      shape([tool("a", "web_search"), tool("b", "web_search")]).latestId,
    ).toBe("b");
  });
});

describe("workShape reports how much is folded", () => {
  test("steps counts the rows expanding would reveal, one per group", () => {
    expect(shape([think("a"), tool("b", "web_search"), think("c")]).steps).toBe(
      3,
    );
  });

  test("batched host commands are one group and so one step", () => {
    // `groupBlocks` batches consecutive host commands into a single card, so
    // counting blocks here would promise two rows and open onto one.
    const s = shape([host("a", "ok"), host("b", "ok")]);
    expect(s.steps).toBe(1);
  });

  test("an empty run has no latest step and no size", () => {
    expect(shape([])).toEqual({ steps: 0 });
  });
});
