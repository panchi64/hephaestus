import { describe, expect, test } from "bun:test";
import {
  decisionsFor,
  isOneClickPark,
  settlesImmediately,
  type Answer,
} from "./approvalDecisions";
import type { Approval } from "./model";

const call = (id: string): Approval => ({
  toolCallId: id,
  name: "files_write_file",
  args: {},
  summary: "",
});

describe("isOneClickPark", () => {
  test("one call and nothing else is one click", () => {
    expect(isOneClickPark(1, 0)).toBe(true);
  });
  test("several calls keep pick-then-submit", () => {
    expect(isOneClickPark(2, 0)).toBe(false);
    expect(isOneClickPark(0, 0)).toBe(false);
  });
  test("a question beside the call still needs the combined submit", () => {
    expect(isOneClickPark(1, 1)).toBe(false);
  });
});

describe("settlesImmediately", () => {
  test("approve and deny settle; a request for changes waits for its note", () => {
    expect(settlesImmediately({ approved: true, intent: "deny" })).toBe(true);
    expect(settlesImmediately({ approved: false, intent: "deny" })).toBe(true);
    expect(settlesImmediately({ approved: false, intent: "revise" })).toBe(
      false,
    );
  });
});

describe("decisionsFor", () => {
  const given: Record<string, Answer> = {
    a: { approved: true, intent: "deny" },
    b: { approved: false, intent: "revise" },
  };
  const scopeOf = (_a: Approval, approved: boolean) =>
    approved ? ("conversation" as const) : ("once" as const);

  test("carries the grant scope on a yes and the intent only on a no", () => {
    expect(decisionsFor([call("a"), call("b")], given, scopeOf)).toEqual([
      { tool_call_id: "a", approved: true, scope: "conversation" },
      { tool_call_id: "b", approved: false, scope: "once", intent: "revise" },
    ]);
  });

  test("leaves out calls not yet answered", () => {
    expect(decisionsFor([call("a"), call("c")], given, scopeOf)).toHaveLength(
      1,
    );
  });
});
