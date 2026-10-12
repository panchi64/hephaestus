import { describe, expect, test } from "bun:test";
import { commandBoundary, toTerminalOutcome } from "./data/hostCommands";
import { isTerminalTool } from "./toolPresentation";

/**
 * Which tools render as a terminal, and how a command's result becomes one.
 *
 * The table is the point. The fold used to ask `name === code_run_host_command` in four
 * separate event cases, so the worktree shell — a worktree thread's only way to run anything at
 * all — fell through to a generic tool card while carrying the terminal glyph.
 *
 * The **second** point is the object/string split. Both command tools answer with a
 * record when they execute and a plain sentence when a guard refused them, so the shape
 * of the result is what says whether anything ran — and nothing here may be keyed on a
 * tool name to work that out.
 */

describe("the table says which calls are terminals", () => {
  test("both tools that run a command", () => {
    expect(isTerminalTool("code_run_host_command")).toBe(true);
    expect(isTerminalTool("shell_run_command")).toBe(true);
  });

  test("and nothing else", () => {
    // `start_command` deliberately included: a background process is a handle the
    // agent checks on later, not output the operator watches arrive.
    expect(isTerminalTool("shell_start_command")).toBe(false);
    expect(isTerminalTool("shell_check_command")).toBe(false);
    expect(isTerminalTool("code_execute")).toBe(false);
    expect(isTerminalTool("files_read_file")).toBe(false);
    expect(isTerminalTool("external_linear_create_issue")).toBe(false);
  });
});

describe("a command that executed answers with a record", () => {
  test("each stream lands in its own slot", () => {
    expect(
      toTerminalOutcome({
        ok: true,
        exit_code: 0,
        stdout: "hello\n",
        stderr: "",
        timed_out: false,
        duration_ms: 42,
        reach: "workspace",
        fenced: true,
      }),
    ).toEqual({
      phase: "ok",
      exitCode: 0,
      stdout: "hello\n",
      stderr: "",
      timedOut: false,
      error: undefined,
      elapsedMs: 42,
      reach: "workspace",
      fenced: true,
      unfencedReason: undefined,
      fenceNote: undefined,
    });
  });

  test("a non-zero exit fails the terminal and keeps what it printed", () => {
    const out = toTerminalOutcome({
      ok: false,
      exit_code: 2,
      stdout: "",
      stderr: "boom\n",
      timed_out: false,
      duration_ms: 7,
      reach: "workspace",
      fenced: true,
      error: "The command exited with status 2.",
    });
    expect(out?.phase).toBe("error");
    expect(out?.exitCode).toBe(2);
    expect(out?.stderr).toBe("boom\n");
  });

  test("a timeout has no exit code, and the null is the fact", () => {
    const out = toTerminalOutcome({
      ok: false,
      exit_code: null,
      stdout: "partial",
      stderr: "",
      timed_out: true,
      duration_ms: 300_000,
      reach: "workspace",
      fenced: true,
      error: "The command was still running after 300.0s and was terminated.",
    });
    expect(out?.phase).toBe("error");
    expect(out?.timedOut).toBe(true);
    // Not `undefined`: the command ran and never exited, which is a different fact
    // from a command that has not run yet.
    expect(out?.exitCode).toBeNull();
  });

  test("an unfenced run carries its reason, and a held fence its note", () => {
    const unfenced = toTerminalOutcome({
      ok: true,
      exit_code: 0,
      stdout: "",
      stderr: "",
      timed_out: false,
      duration_ms: 3,
      reach: "workspace",
      fenced: false,
      unfenced_reason: "This host has no sandbox primitive.",
    });
    expect(unfenced?.fenced).toBe(false);
    expect(unfenced?.unfencedReason).toBe(
      "This host has no sandbox primitive.",
    );

    const denied = toTerminalOutcome({
      ok: false,
      exit_code: 1,
      stdout: "",
      stderr: "touch: /etc/hosts: Operation not permitted\n",
      timed_out: false,
      duration_ms: 5,
      reach: "workspace",
      fenced: true,
      fence_note: "The fence denied a write outside the worktree.",
    });
    expect(denied?.fenceNote).toBe(
      "The fence denied a write outside the worktree.",
    );
  });

  test("a command that printed nothing still reports its exit", () => {
    const out = toTerminalOutcome({
      ok: true,
      exit_code: 0,
      stdout: "",
      stderr: "",
      timed_out: false,
      duration_ms: 1,
      reach: "workspace",
      fenced: true,
    });
    // Empty strings, never a "(no output)" sentinel the card would have to recognise.
    expect(out?.stdout).toBe("");
    expect(out?.phase).toBe("ok");
  });
});

describe("a plain string means it never ran", () => {
  test("a refusal yields no outcome at all", () => {
    // `toHostCommand` and the live fold both turn this null into a *denied* terminal
    // rather than a green OK, which is the whole reason the shape is the test.
    expect(
      toTerminalOutcome("Shell commands are not available in this mode."),
    ).toBeNull();
  });

  test("and so does a shape this build does not know", () => {
    expect(toTerminalOutcome({ something: "else" })).toBeNull();
    expect(toTerminalOutcome(null)).toBeNull();
  });
});

describe("a command that is not a terminal still declared a reach", () => {
  test("a backgrounded process carries its declaration and fence", () => {
    // `shell_start_command` renders as an ordinary tool card — its result is a handle,
    // not output — and without this the one row for a long-lived process the agent
    // launched on the operator's machine would say nothing about how far it may go.
    expect(
      commandBoundary({
        ok: true,
        command_id: "bg-1",
        command: "bun run dev",
        reach: "network",
        fenced: false,
        unfenced_reason: "This host has no sandbox primitive.",
      }),
    ).toEqual({
      reach: "network",
      fenced: false,
      unfencedReason: "This host has no sandbox primitive.",
      fenceNote: undefined,
    });
  });

  test("and everything else carries none", () => {
    // Keyed on the presence of a declaration, never on a tool name — so a tool that
    // does not run a command cannot accidentally grow a boundary row.
    expect(commandBoundary({ ok: true, entries: ["a", "b"] })).toBeUndefined();
    expect(commandBoundary("a sentence")).toBeUndefined();
    expect(commandBoundary(undefined)).toBeUndefined();
  });
});
