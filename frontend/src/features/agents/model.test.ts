import { describe, expect, it } from "bun:test";
import {
  agentState,
  agentToSend,
  agentSourceTag,
  compareAgentsBySource,
  isAwaitingPublish,
  pickableAgents,
  threadAgentName,
  type Agent,
} from "./model";

function agent(over: Partial<Agent> = {}): Agent {
  return {
    id: "a1",
    name: "reviewer",
    description: "Reviews diffs",
    instructions: "Be terse.",
    endpointId: null,
    model: null,
    tools: null,
    denyTools: [],
    permissionCeiling: null,
    workspace: "shared",
    requestLimit: null,
    wallClockTimeoutS: null,
    canDelegate: false,
    maxDepth: null,
    delegable: true,
    source: "user",
    published: true,
    active: true,
    editable: true,
    ...over,
  };
}

describe("agentSourceTag", () => {
  it("names every provenance except the operator's own", () => {
    expect(agentSourceTag("builtin")).toBe("BUILT-IN");
    expect(agentSourceTag("project")).toBe("PROJECT");
    expect(agentSourceTag("agent")).toBe("AGENT-DRAFTED");
    expect(agentSourceTag("user")).toBeNull();
  });
});

describe("compareAgentsBySource", () => {
  it("puts built-in first and agent-drafted after the operator's own", () => {
    const sorted = [
      agent({ id: "1", source: "agent" }),
      agent({ id: "2", source: "user" }),
      agent({ id: "3", source: "builtin" }),
    ].sort(compareAgentsBySource);
    expect(sorted.map((a) => a.source)).toEqual(["builtin", "user", "agent"]);
  });
});

describe("agentState", () => {
  it("reads unpublished as draft, shadowed only when published but inactive", () => {
    expect(agentState(agent())).toBe("published");
    expect(agentState(agent({ published: false, active: false }))).toBe(
      "draft",
    );
    expect(agentState(agent({ active: false }))).toBe("shadowed");
  });

  it("flags only agent-authored drafts as awaiting the operator", () => {
    expect(
      isAwaitingPublish(agent({ source: "agent", published: false })),
    ).toBe(true);
    expect(isAwaitingPublish(agent({ source: "agent", published: true }))).toBe(
      false,
    );
    expect(isAwaitingPublish(agent({ source: "user", published: false }))).toBe(
      false,
    );
  });
});

describe("pickableAgents", () => {
  it("puts default first and keeps only published, active agents", () => {
    const list = pickableAgents([
      agent({ id: "1", name: "zeta" }),
      agent({ id: "2", name: "draft", published: false, active: false }),
      agent({ id: "3", name: "default", source: "builtin" }),
      agent({ id: "4", name: "alpha" }),
      agent({ id: "5", name: "shadow", active: false }),
    ]);
    expect(list.map((a) => a.name)).toEqual(["default", "alpha", "zeta"]);
  });
});

describe("agentToSend", () => {
  it("sends nothing until the operator picks", () => {
    expect(
      agentToSend({ choice: undefined, sent: undefined, isNewThread: false }),
    ).toBeUndefined();
  });

  it("omits the default on a new thread but names it on an existing one", () => {
    expect(
      agentToSend({ choice: "default", sent: undefined, isNewThread: true }),
    ).toBeUndefined();
    expect(
      agentToSend({ choice: "default", sent: undefined, isNewThread: false }),
    ).toBe("default");
  });

  it("does not repeat a choice a send already carried", () => {
    expect(
      agentToSend({ choice: "reviewer", sent: "reviewer", isNewThread: false }),
    ).toBeUndefined();
    expect(
      agentToSend({ choice: "other", sent: "reviewer", isNewThread: false }),
    ).toBe("other");
  });

  it("reads a null stored agent as the default", () => {
    expect(threadAgentName(null)).toBe("default");
    expect(threadAgentName(undefined)).toBe("default");
    expect(threadAgentName("reviewer")).toBe("reviewer");
  });
});
