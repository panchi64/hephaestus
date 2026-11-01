import { describe, expect, test } from "bun:test";
import { describeToolArgs, describeToolResult } from "./toolSummary";

describe("what the call was about", () => {
  test("the table names the argument that distinguishes one call from the next", () => {
    expect(
      describeToolArgs("files_read_file", { path: "backend/app.py" }),
    ).toBe("backend/app.py");
    expect(
      describeToolArgs("shell_run_command", {
        command: "bun test",
        timeout: 60,
      }),
    ).toBe("bun test");
    // `explanation` beats `command` here — the operator wrote the tool to say why.
    expect(
      describeToolArgs("shell_run_on_host", {
        command: "pytest -q",
        explanation: "Run the backend suite",
      }),
    ).toBe("Run the backend suite");
  });

  test("an unlisted tool falls back to a generic preference order", () => {
    expect(
      describeToolArgs("external_linear_create_issue", {
        team: "core",
        title: "Ship the rail",
      }),
    ).toBe("Ship the rail");
  });

  // The row shows the full `k=v` summary when this returns nothing, so a miss
  // costs nothing — which is why guessing is not worth it.
  test("nothing salient means nothing said", () => {
    expect(describeToolArgs("mail_mark", { seen: true })).toBeUndefined();
    expect(describeToolArgs("builtin_now", {})).toBeUndefined();
  });

  test("a multi-line value becomes its first non-empty line", () => {
    expect(
      describeToolArgs("code_execute", {
        code: "\n\nimport sys\nprint(sys.version)\n",
      }),
    ).toBe("import sys");
  });

  test("a list argument reads as its members", () => {
    expect(
      describeToolArgs("mail_send", { to: ["a@example.com", "b@example.com"] }),
    ).toBe("a@example.com, b@example.com");
  });

  // Which groups the model asked for is the only thing that distinguishes one of
  // these calls from the next, and the argument carrying them is a list.
  test("loading tools says which groups were asked for", () => {
    expect(describeToolArgs("search_tools", { queries: ["browse"] })).toBe(
      "browse",
    );
    expect(
      describeToolArgs("search_tools", { queries: ["mail", "calendar"] }),
    ).toBe("mail, calendar");
  });

  test("a long value is clamped rather than allowed to run", () => {
    const detail = describeToolArgs("web_search", { query: "x".repeat(400) });
    expect(detail).toHaveLength(120);
    expect(detail!.endsWith("…")).toBe(true);
  });
});

describe("what came back", () => {
  test("a collection is counted in the noun the tool deals in", () => {
    expect(describeToolResult("memory_recall", [{}, {}, {}])).toBe(
      "3 memories",
    );
    expect(describeToolResult("memory_recall", [{}])).toBe("1 memory");
    expect(describeToolResult("files_find_files", [{}, {}])).toBe("2 matches");
    expect(describeToolResult("memory_audit", [{}, {}])).toBe("2 groups");
    expect(
      describeToolResult("memory_list", { total: 12, memories: [{}, {}] }),
    ).toBe("2 memories");
  });

  test("a revealed group is counted in tools, not in bare results", () => {
    expect(describeToolResult("search_tools", [{}, {}, {}])).toBe("3 tools");
    expect(describeToolResult("search_tools", [{}])).toBe("1 tool");
  });

  test("a tool with no noun of its own counts results", () => {
    expect(describeToolResult("external_linear_list_issues", [{}, {}])).toBe(
      "2 results",
    );
  });

  test("a command reports its exit status", () => {
    expect(
      describeToolResult("code_execute", { exit_code: 0, stdout: "" }),
    ).toBe("exit 0");
    expect(
      describeToolResult("code_execute", { exit_code: 124, timed_out: true }),
    ).toBe("exit 124 · timed out");
  });

  // A tool that returns `{error: ...}` still *completed* — the card's error
  // branch never fires, so the collapsed row is the only place this shows.
  test("a reported failure inside a successful call is surfaced", () => {
    expect(
      describeToolResult("calendar_agenda", { error: "Calendar unavailable." }),
    ).toBe("Calendar unavailable.");
  });

  test("a wrapper object is counted by the one list inside it", () => {
    expect(
      describeToolResult("mail_list_messages", {
        account_id: "a1",
        messages: [{}, {}, {}, {}],
      }),
    ).toBe("4 messages");
  });

  test("a short answer is simply shown; a long one is measured", () => {
    expect(describeToolResult("builtin_now", "2026-08-30T09:35:00Z")).toBe(
      "2026-08-30T09:35:00Z",
    );
    expect(describeToolResult("files_read_file", "one\ntwo\nthree\nfour")).toBe(
      "4 lines",
    );
  });

  // The count is taken without splitting the payload, so the blank edges a file
  // read carries have to be excluded by hand rather than by `trim()`.
  test("blank edges do not inflate the line count", () => {
    expect(describeToolResult("files_read_file", "one\ntwo\n")).toBe("2 lines");
    expect(describeToolResult("files_read_file", "\n\n  one\ntwo\n\n  ")).toBe(
      "2 lines",
    );
    expect(describeToolResult("builtin_now", "\n  2026-08-30  \n")).toBe(
      "2026-08-30",
    );
  });

  test("an empty or shapeless result says nothing", () => {
    expect(describeToolResult("files_write_file", "   ")).toBeUndefined();
    expect(describeToolResult("view_close", null)).toBeUndefined();
    expect(describeToolResult("mail_mark", true)).toBeUndefined();
    // Two lists and no count — nothing to point at without picking a winner.
    expect(
      describeToolResult("project_list", { projects: [{}], recent: [{}, {}] }),
    ).toBeUndefined();
  });
});
