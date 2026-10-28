import { describe, expect, test } from "bun:test";
import {
  WORK_LOG_MIN_RUN,
  layoutItemKey,
  liveToolGroupIds,
  groupBlocks,
  planTurnLayout,
  runningTools,
  workLogStateKey,
} from "./blocks";
import type {
  AssistantBlock,
  HostCommandPhase,
  ToolImage,
  ToolStatus,
} from "./model";

/** A tool block, named so an assertion can point at it by name. */
function tool(id: string, name: string, status: ToolStatus): AssistantBlock {
  return { kind: "tool", id, tool: { id, name, args: "", status } };
}

/** A settled tool call that came back with a screenshot — or, given an empty list, one
 *  that reported no pictures at all. */
function shot(
  id: string,
  images: ToolImage[] = [{ mediaType: "image/png", data: "QUJD" }],
): AssistantBlock {
  return {
    kind: "tool",
    id,
    tool: { id, name: "browse_screenshot", args: "", status: "ok", images },
  };
}

function text(id: string): AssistantBlock {
  return { kind: "text", id, text: "done" };
}

/** A passage the model emitted with nothing in it — the bare newline that lands
 *  between two batches of tool calls. */
function blank(id: string, body = "\n\n"): AssistantBlock {
  return { kind: "text", id, text: body };
}

/** The chip that points at a captured version. */
function chip(id: string): AssistantBlock {
  return { kind: "view_version", id, snapshotId: `s-${id}`, title: "v1" };
}

function host(id: string, phase: HostCommandPhase): AssistantBlock {
  return {
    kind: "host_command",
    id,
    command: {
      toolCallId: id,
      name: "shell_run_on_host",
      command: "ls",
      phase,
    },
  };
}

/** The layout of a turn, as the kinds it puts on the rail — "worklog" for a fold,
 *  the group's own id for anything left inline. Defaults to a streaming turn;
 *  pass `false` for a settled one, which is the harder case for any rule that
 *  keeps something visible — `planTurnLayout`'s own tail guard only runs while
 *  streaming, so a settled turn has nothing else propping the block up. */
function plan(blocks: AssistantBlock[], streaming = true): string[] {
  return planTurnLayout(groupBlocks(blocks), { streaming }).map((item) =>
    item.type === "worklog" ? "worklog" : item.group.id,
  );
}

/** Every row of every work log in a turn, as `id` or `id!` when the fold may not
 *  hide it.
 *
 *  This is the assertion that replaced "the log splits here". Staying visible and
 *  ending the log used to be the same act, so the tests below could only say a
 *  failure was protected by showing a second log after it. They say it directly
 *  now, which is also the thing that would silently stop being true. */
function rows(blocks: AssistantBlock[], streaming = true): string[] {
  return planTurnLayout(groupBlocks(blocks), { streaming }).flatMap((item) =>
    item.type === "worklog"
      ? item.entries.map((e) => `${e.group.id}${e.pinned ? "!" : ""}`)
      : [],
  );
}

describe("parallel tool calls stay visible while they run", () => {
  // These fixtures FIGHT the rule: `planTurnLayout` already keeps the trailing group
  // inline while streaming, so a running call in last position would pass with the
  // guard deleted. Each one puts it mid-run instead, with enough collapsible groups
  // around it that dropping the guard really does fold a work log over it.
  test("a batch with one call still running is pinned open in place", () => {
    expect(WORK_LOG_MIN_RUN).toBe(1);
    const blocks = [
      tool("a", "web_search", "ok"),
      tool("b", "web_search", "running"),
      tool("c", "web_search", "ok"),
      tool("d", "web_search", "ok"),
    ];
    // Guard removed: `b` folds away with the rest.
    expect(rows(blocks)).toEqual(["a", "b!", "c", "d!"]);
    // And the whole batch is still ONE account of the turn.
    expect(plan(blocks)).toEqual(["worklog"]);
  });

  test("a whole batch still running is every row pinned, not no log", () => {
    // The trailing block is the answer, so no tool here is protected by position.
    const blocks = [
      tool("a", "web_search", "running"),
      tool("b", "web_search", "running"),
      tool("c", "web_search", "running"),
      tool("d", "web_search", "running"),
      text("t"),
    ];
    expect(rows(blocks)).toEqual(["a!", "b!", "c!", "d!"]);
    expect(plan(blocks)).toEqual(["worklog", "t"]);
  });

  test("the same batch, all settled, folds as before", () => {
    // The counterpart: a cleanly finished batch must still recede into the work
    // log. (Every call here is `ok` on purpose — a failure among them would be
    // pinned inline by its own rule, which is the next describe block.)
    expect(
      plan([
        tool("a", "web_search", "ok"),
        tool("b", "web_search", "ok"),
        tool("c", "web_search", "ok"),
        text("t"),
      ]),
    ).toEqual(["worklog", "t"]);
  });

  test("a fold keys on its place among the turn's folds, not its first row", () => {
    const blocks = [
      tool("a", "web_search", "ok"),
      tool("b", "web_search", "ok"),
      text("t"),
      tool("c", "web_search", "ok"),
      text("u"),
    ];
    const keys = planTurnLayout(groupBlocks(blocks)).map(layoutItemKey);
    expect(keys).toEqual(["w:0", "g:t", "w:1", "g:u"]);
    // Dropping the log's first row (a reseat losing live-only context) keeps the key.
    const reseated = planTurnLayout(groupBlocks(blocks.slice(1)));
    expect(layoutItemKey(reseated[0])).toBe("w:0");
  });

  test("a log's open state keys on its first row a reload keeps", () => {
    const context: AssistantBlock = {
      kind: "context",
      id: "ctx-1",
      injection: {
        contributor: "plan",
        placement: "prompt",
        tokens: 1,
        text: "",
        truncated: false,
      },
    };
    const logs = (blocks: AssistantBlock[]) =>
      planTurnLayout(groupBlocks(blocks)).flatMap((item) =>
        item.type === "worklog" ? [workLogStateKey(item)] : [],
      );
    const live = logs([
      context,
      text("t"),
      tool("c", "web_search", "ok"),
      text("u"),
    ]);
    const reseated = logs([
      text("t"),
      tool("c", "web_search", "ok"),
      text("u"),
    ]);
    expect(live).toEqual(["w:0", "g:c"]);
    expect(reseated).toEqual(["g:c"]);
  });
});

describe("a call that came back with a picture keeps its run inline", () => {
  // The defect this covers bites hardest in the case that produces the pictures: an
  // agent that screenshots repeatedly makes a *run* of settled calls, which is exactly
  // what folds — so the images would be hidden precisely when there are the most of
  // them to see. Same fixture discipline as the failure rule: mid-run, settled turn,
  // enough collapsible groups either side that dropping the guard really does fold.
  test("a settled turn with a screenshot keeps it pinned open", () => {
    const blocks = [
      tool("a", "browse_click", "ok"),
      shot("b"),
      tool("c", "browse_get_text", "ok"),
      tool("d", "browse_click", "ok"),
    ];
    expect(rows(blocks, false)).toEqual(["a", "b!", "c", "d"]);
    expect(plan(blocks, false)).toEqual(["worklog"]);
  });

  test("an empty image list is not a picture", () => {
    // The rule reads `images?.length`, not `images !== undefined` — a call that
    // reported an empty list has nothing to show and must fold like any other.
    expect(
      rows(
        [
          tool("a", "browse_click", "ok"),
          shot("b", []),
          tool("c", "browse_click", "ok"),
        ],
        false,
      ),
    ).toEqual(["a", "b", "c"]);
  });
});

describe("only a platform failure keeps its run inline", () => {
  /** A call that failed through the platform's own fault. */
  function broken(id: string): AssistantBlock {
    return {
      kind: "tool",
      id,
      tool: {
        id,
        name: "files_read_file",
        args: "",
        status: "error",
        platform: true,
      },
    };
  }

  // Each fixture puts the failure MID-RUN with enough collapsible groups around it
  // that dropping the guard really does fold over it.
  test("a settled turn with a platform failure keeps it pinned open", () => {
    // Nothing is streaming, so `planTurnLayout`'s trailing-group guard is not
    // running and only the failure rule is holding this open.
    expect(
      rows(
        [
          tool("a", "web_search", "ok"),
          broken("b"),
          tool("c", "web_search", "ok"),
          tool("d", "web_search", "ok"),
        ],
        false,
      ),
    ).toEqual(["a", "b!", "c", "d"]);
  });

  test("a platform failure mid-stream does not cut the log in two", () => {
    const blocks = [
      tool("a", "web_search", "ok"),
      broken("b"),
      tool("c", "web_search", "ok"),
      text("t"),
    ];
    expect(plan(blocks)).toEqual(["worklog", "t"]);
    expect(rows(blocks)).toEqual(["a", "b!", "c"]);
  });

  test("an ordinary tool failure folds with the rest of the work", () => {
    expect(
      rows(
        [
          tool("a", "web_search", "ok"),
          tool("b", "files_read_file", "error"),
          tool("c", "web_search", "ok"),
          tool("d", "web_search", "ok"),
        ],
        false,
      ),
    ).toEqual(["a", "b", "c", "d"]);
  });

  test("a failed host command folds like an ordinary failure", () => {
    expect(
      rows(
        [
          tool("a", "web_search", "ok"),
          host("b", "error"),
          tool("c", "web_search", "ok"),
          tool("d", "web_search", "ok"),
        ],
        false,
      ),
    ).toEqual(["a", "b", "c", "d"]);
  });

  test("a denied host command is a decision, not a failure, and folds", () => {
    // The operator already dealt with this one; keeping it pinned forever would
    // mean every refusal permanently un-folds the turn it happened in.
    expect(
      rows(
        [
          tool("a", "web_search", "ok"),
          host("b", "denied"),
          tool("c", "web_search", "ok"),
          tool("d", "web_search", "ok"),
        ],
        false,
      ),
    ).toEqual(["a", "b", "c", "d"]);
  });
});

describe("a blank passage does not segment the log", () => {
  // The defect these cover, and it was the largest source of work leaking out of a
  // fold: a model routinely emits a bare newline between two batches of calls, which
  // became a `text` group — a hard run-breaker that then rendered nothing. One work
  // log turned into two with no visible cause between them, and whatever the second
  // run had too few groups for sat loose on the rail. Every fixture here puts the
  // blank MID-RUN, so restoring the old rule really does split the log.
  test("work either side of a blank stays one log", () => {
    expect(
      plan(
        [
          tool("a", "web_search", "ok"),
          blank("gap"),
          tool("b", "files_read_file", "ok"),
          tool("c", "web_search", "ok"),
        ],
        false,
      ),
    ).toEqual(["worklog"]);
  });

  test("the blank itself is not a row", () => {
    const items = planTurnLayout(
      groupBlocks([tool("a", "web_search", "ok"), blank("gap")]),
      { streaming: false },
    );
    expect(items.map((i) => i.type)).toEqual(["worklog"]);
    expect(items[0].type === "worklog" && items[0].entries.length).toBe(1);
  });

  test("whitespace of any shape counts as blank", () => {
    // `\n\n` is the common one, but a lone space and a tab are the same nothing.
    expect(
      plan(
        [
          tool("a", "web_search", "ok"),
          blank("gap", " \t "),
          tool("b", "x", "ok"),
        ],
        false,
      ),
    ).toEqual(["worklog"]);
  });

  test("a passage with words in it still splits the log", () => {
    // The counterpart that proves the rule reads *emptiness*, not the `text` kind:
    // an answer the operator reads is still the one thing that segments a log.
    expect(
      plan(
        [
          tool("a", "web_search", "ok"),
          text("t"),
          tool("b", "files_read_file", "ok"),
        ],
        false,
      ),
    ).toEqual(["worklog", "t", "worklog"]);
  });

  test("the streaming tail keeps its blank, because it carries the caret", () => {
    // An answer's first block is blank for exactly as long as its first delta takes
    // to arrive. Dropping it would blink the caret out at the head of every answer,
    // so the live tail is the one blank that still renders.
    expect(plan([tool("a", "web_search", "ok"), blank("t")])).toEqual([
      "worklog",
      "t",
    ]);
  });
});

describe("a lone run folds, but a View chip never does", () => {
  test("one settled call is a work log of its own", () => {
    // The floor is one: everything `isCollapsible` admits has already had its chance
    // to pin itself inline, so a remainder left on the rail reads as leakage rather
    // than as something kept deliberately visible.
    expect(plan([tool("a", "web_search", "ok"), text("t")], false)).toEqual([
      "worklog",
      "t",
    ]);
  });

  test("a chip stays inline even alone between two logs", () => {
    // A chip is a result, not process — and the transcript's one handle into the
    // viewport. At a floor of one it would otherwise become `Work log · 1 step`
    // with the handle behind a disclosure.
    expect(
      plan(
        [
          tool("a", "web_search", "ok"),
          chip("v1"),
          tool("b", "files_read_file", "ok"),
        ],
        false,
      ),
    ).toEqual(["worklog", "v1", "worklog"]);
  });
});

describe("liveToolGroupIds", () => {
  test("names every group with a call in flight, not just the last", () => {
    const groups = groupBlocks([
      tool("a", "web_search", "running"),
      tool("b", "web_fetch", "ok"),
      tool("c", "code_execute", "running"),
    ]);
    expect(liveToolGroupIds(groups)).toEqual(new Set(["a", "c"]));
  });

  test("is empty once the batch settles", () => {
    const groups = groupBlocks([
      tool("a", "web_search", "ok"),
      tool("b", "web_fetch", "error"),
    ]);
    expect(liveToolGroupIds(groups).size).toBe(0);
  });
});

describe("injected context recedes into the fold", () => {
  /** One block the chassis put in front of the model. */
  function injected(id: string, contributor: string): AssistantBlock {
    return {
      kind: "context",
      id,
      injection: {
        contributor,
        placement: "instructions",
        tokens: 120,
        text: "…",
        truncated: false,
      },
    };
  }

  test("a turn's preamble folds instead of leading with itself", () => {
    // Every turn opens with a clump of these. Left inline they would push the answer
    // down the page behind three rows of frame — and unlike a tool call there is
    // nothing in them to watch, act on, or wait for.
    expect(
      plan(
        [
          injected("c1", "repo"),
          injected("c2", "skill_catalog"),
          injected("c3", "date"),
          text("t"),
        ],
        false,
      ),
    ).toEqual(["worklog", "t"]);
  });

  test("it joins the work after it rather than forming its own log", () => {
    // The preamble and the calls it framed are ONE run, not an injection log
    // followed by a work log — which is what a per-kind flush would give.
    const items = planTurnLayout(
      groupBlocks([
        injected("c1", "repo"),
        tool("a", "files_read_file", "ok"),
        tool("b", "web_search", "ok"),
        text("t"),
      ]),
      { streaming: false },
    );
    expect(items.map((i) => i.type)).toEqual(["worklog", "group"]);
    expect(items[0].type === "worklog" && items[0].entries.length).toBe(3);
  });

  test("a live call is still pinned open beside an injection", () => {
    // The pin rules are about the *work*, and an injection must not smother them: a
    // call still in flight stays on screen with its spinner whatever sits beside it.
    expect(
      rows([injected("c1", "repo"), tool("a", "web_search", "running")], false),
    ).toEqual(["c1", "a!"]);
  });
});

describe("runningTools", () => {
  test("collects in-flight calls across the turn, in order", () => {
    // The running call is first and the trailing block isn't one — the case a tail-only
    // read gets wrong.
    expect(
      runningTools([
        tool("a", "web_search", "running"),
        tool("b", "web_fetch", "ok"),
        text("t"),
      ]).map((t) => t.name),
    ).toEqual(["web_search"]);
  });

  test("returns the whole batch when several are out", () => {
    expect(
      runningTools([
        tool("a", "web_search", "running"),
        tool("b", "web_fetch", "running"),
      ]),
    ).toHaveLength(2);
  });

  test("counts a call still preparing its arguments", () => {
    expect(
      runningTools([
        tool("a", "web_search", "preparing"),
        tool("b", "web_fetch", "running"),
      ]),
    ).toHaveLength(2);
  });

  test("is empty for a turn with no tools", () => {
    expect(runningTools([text("t")])).toEqual([]);
    expect(runningTools(undefined)).toEqual([]);
  });
});

describe("a review folds with the work, unless it refused something", () => {
  /** One call the chassis ruled on in the operator's place. */
  function review(
    id: string,
    decision?: "allow" | "ask" | "block",
  ): AssistantBlock {
    return {
      kind: "review",
      id,
      review: {
        toolCallId: id,
        name: "shell_run_command",
        summary: "Runs the shell command: git status",
        decision,
        stage: "judge",
        reason: "reads the workspace and changes nothing",
      },
    };
  }

  test("a cleared call's review recedes like the work it cleared", () => {
    // Nothing about it needs the operator *now*: the call it cleared ran, and the
    // account of why is one fold away.
    expect(
      plan(
        [
          review("r1", "allow"),
          tool("a", "shell_run_command", "ok"),
          tool("b", "files_read_file", "ok"),
          text("t"),
        ],
        false,
      ),
    ).toEqual(["worklog", "t"]);
  });

  test("a refusal stays on screen however much settles around it", () => {
    // The single thing in a turn the operator is most likely to disagree with, and the
    // only one with no following row to account for it — a refused call is followed by
    // nothing at all. Burying it would make Auto's promise unverifiable in practice.
    const blocks = [
      tool("a", "files_read_file", "ok"),
      tool("b", "files_read_file", "ok"),
      tool("c", "files_read_file", "ok"),
      review("r1", "block"),
      tool("d", "files_read_file", "ok"),
      tool("e", "files_read_file", "ok"),
      tool("f", "files_read_file", "ok"),
      text("t"),
    ];
    expect(rows(blocks, false)).toEqual(["a", "b", "c", "r1!", "d", "e", "f"]);
    expect(plan(blocks, false)).toEqual(["worklog", "t"]);
  });

  test("a review still in flight folds rather than pinning the turn open", () => {
    // It resolves on its own in a second or two, and a row that pins the log open for
    // every reviewed call would leave an Auto thread permanently unfolded.
    expect(
      plan(
        [
          review("r1"),
          tool("a", "files_read_file", "ok"),
          tool("b", "files_read_file", "ok"),
          text("t"),
        ],
        false,
      ),
    ).toEqual(["worklog", "t"]);
  });
});
