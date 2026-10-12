import { describe, expect, it } from "bun:test";
import {
  agentState,
  agentToSend,
  buildAgentPatch,
  draftOf,
  draftToWrite,
  groupAgentsBySource,
  isAwaitingPublish,
  isDraftDirty,
  parseLimit,
  parseList,
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

describe("groupAgentsBySource", () => {
  it("orders groups built-in, project, yours, agent-drafted and drops empty ones", () => {
    const groups = groupAgentsBySource([
      agent({ id: "1", name: "z", source: "agent" }),
      agent({ id: "2", name: "default", source: "builtin" }),
      agent({ id: "3", name: "mine", source: "user" }),
    ]);
    expect(groups.map((g) => g.label)).toEqual([
      "Built-in",
      "Yours",
      "Drafted by agents",
    ]);
    expect(groups[0].agents.map((a) => a.name)).toEqual(["default"]);
  });

  it("returns nothing for an empty list", () => {
    expect(groupAgentsBySource([])).toEqual([]);
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

describe("lists and limits", () => {
  it("parses comma and newline lists, trimming and de-duplicating", () => {
    expect(parseList(" a, b ,,a\nc ")).toEqual(["a", "b", "c"]);
    expect(parseList("")).toEqual([]);
  });

  it("clears on blank and omits on non-numeric text", () => {
    expect(parseLimit("")).toBeNull();
    expect(parseLimit(" 30 ")).toBe(30);
    expect(parseLimit("abc")).toBeUndefined();
  });
});

describe("buildAgentPatch", () => {
  it("is empty for an untouched draft", () => {
    const a = agent({ tools: ["read"], denyTools: ["bash"], requestLimit: 5 });
    expect(buildAgentPatch(draftOf(a), a)).toEqual({});
    expect(isDraftDirty(draftOf(a), a)).toBe(false);
  });

  it("sends only the fields that changed, snake_case", () => {
    const a = agent();
    const draft = { ...draftOf(a), description: "New", canDelegate: true };
    expect(buildAgentPatch(draft, a)).toEqual({
      description: "New",
      can_delegate: true,
    });
  });

  it("sends explicit nulls to clear a field", () => {
    const a = agent({
      tools: ["read"],
      permissionCeiling: "edit",
      requestLimit: 10,
      endpointId: "e1",
      model: "m",
    });
    const draft = {
      ...draftOf(a),
      allTools: true,
      permissionCeiling: null,
      requestLimit: "",
      endpointId: null,
      model: null,
    };
    expect(buildAgentPatch(draft, a)).toEqual({
      tools: null,
      permission_ceiling: null,
      request_limit: null,
      endpoint_id: null,
      model: null,
    });
  });

  it("sends the allow-list as an array once all-tools is off", () => {
    const a = agent();
    const draft = { ...draftOf(a), allTools: false, toolsText: "read, grep" };
    expect(buildAgentPatch(draft, a)).toEqual({ tools: ["read", "grep"] });
  });

  it("leaves a half-typed number out instead of coercing it", () => {
    const a = agent({ requestLimit: 3 });
    const draft = { ...draftOf(a), requestLimit: "x" };
    expect(draftToWrite(draft).request_limit).toBeUndefined();
    expect(buildAgentPatch(draft, a)).toEqual({});
  });
});
