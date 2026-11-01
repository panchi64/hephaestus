import { describe, expect, test } from "bun:test";
import type { Subagent, SubagentStatus } from "./data/subagents";
import { stopRoute } from "./stopRoute";

const agent = (status: SubagentStatus): Subagent => ({ status }) as Subagent;

describe("what Stop does", () => {
  test("a live run is cancelled, which stops its agents too", () => {
    expect(stopRoute({ sending: true, subagents: [agent("running")] })).toBe(
      "run",
    );
  });

  test("an idle thread with a live agent stops the agents", () => {
    expect(stopRoute({ sending: false, subagents: [agent("running")] })).toBe(
      "agents",
    );
    expect(stopRoute({ sending: false, subagents: [agent("blocked")] })).toBe(
      "agents",
    );
  });

  test("an idle thread whose agents have all finished has nothing to stop", () => {
    expect(
      stopRoute({
        sending: false,
        subagents: [agent("done"), agent("stopped"), agent("cancelled")],
      }),
    ).toBe("none");
    expect(stopRoute({ sending: false, subagents: [] })).toBe("none");
  });
});
