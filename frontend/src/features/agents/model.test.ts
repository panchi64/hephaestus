import { describe, expect, it } from "bun:test";
import {
  agentHierarchy,
  agentState,
  agentToSend,
  agentSourceTag,
  callsignSummary,
  compareAgentsBySource,
  delegationSummary,
  isAwaitingPublish,
  launchedBySummary,
  agentDisplayName,
  classDisplay,
  pickableAgents,
  threadAgentName,
  type Agent,
  type AgentClass,
} from "./model";

const cls = (key: string, callsigns: string[] = []): AgentClass => ({
  key,
  display: key.charAt(0).toUpperCase() + key.slice(1),
  role: `${key} work`,
  callsigns,
  assignable: key !== "keledones",
  root: key === "talos",
});

const CLASSES = [
  cls("talos"),
  cls("kourai", ["KYRĒ", "LYXĒ", "MYRĒ", "SYLĒ"]),
  cls("tripodes", ["TIKK"]),
  cls("khryseos", ["SKAR"]),
  cls("keledones"),
];

describe("agentHierarchy", () => {
  it("puts Talos at the root with default first, then classes in table order", () => {
    const agents = [
      agent({ name: "worker", agentClass: "tripodes" }),
      agent({ name: "concierge", agentClass: "talos" }),
      agent({ name: "reviewer", agentClass: "khryseos" }),
      agent({ name: "default", agentClass: "talos" }),
      agent({ name: "explorer", agentClass: "kourai" }),
    ];
    const groups = agentHierarchy(agents, CLASSES);
    expect(groups.map((g) => g.key)).toEqual([
      "talos",
      "kourai",
      "tripodes",
      "khryseos",
    ]);
    expect(groups[0]!.items.map((a) => a.name)).toEqual([
      "default",
      "concierge",
    ]);
    expect(groups[1]!.note).toBe("kourai work");
  });

  it("keeps the caller's order inside a group and files unknown classes last", () => {
    const groups = agentHierarchy(
      [
        agent({ name: "b", agentClass: "kourai" }),
        agent({ name: "odd", agentClass: "mystery" }),
        agent({ name: "a", agentClass: "kourai" }),
      ],
      CLASSES,
    );
    expect(groups.map((g) => g.key)).toEqual(["kourai", "mystery"]);
    expect(groups[0]!.items.map((a) => a.name)).toEqual(["b", "a"]);
  });
});

describe("class summaries", () => {
  it("shows three callsign stems, and none for Talos or an unaddressed class", () => {
    expect(callsignSummary(CLASSES[1])).toBe("KYRĒ-nn · LYXĒ-nn · MYRĒ-nn · …");
    expect(callsignSummary(CLASSES[0])).toBe("None. Addressed as Talos");
    expect(callsignSummary(CLASSES[4])).toBe("None. Not addressed");
  });

  it("says who launches an agent, and why no one can", () => {
    expect(launchedBySummary(agent())).toBe(
      "Talos, or any agent that can delegate",
    );
    expect(
      launchedBySummary(agent({ delegable: false, launchable: false })),
    ).toBe("Not launched by other agents");
    expect(
      launchedBySummary(agent({ published: false, launchable: false })),
    ).toBe("Not launched until published");
    expect(launchedBySummary(agent({ active: false, launchable: false }))).toBe(
      "Not launched: a same-named agent takes precedence",
    );
  });

  it("caps the depth at the installation's limit", () => {
    expect(delegationSummary(agent(), 3)).toBe("No");
    expect(delegationSummary(agent({ canDelegate: true }), 3)).toBe(
      "Yes · up to 3 levels below",
    );
    expect(
      delegationSummary(agent({ canDelegate: true, maxDepth: 1 }), 3),
    ).toBe("Yes · up to 1 level below");
    expect(
      delegationSummary(agent({ canDelegate: true, maxDepth: 5 }), 3),
    ).toBe("Yes · up to 3 levels below");
  });
});

function agent(over: Partial<Agent> = {}): Agent {
  return {
    id: "a1",
    name: "reviewer",
    description: "Reviews diffs",
    agentClass: "khryseos",
    instructions: "Be terse.",
    endpointId: null,
    model: null,
    tools: null,
    denyTools: [],
    permissionCeiling: null,
    workspace: "shared",
    wallClockTimeoutS: null,
    canDelegate: false,
    maxDepth: null,
    delegable: true,
    launchable: true,
    required: [],
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

describe("agentDisplayName", () => {
  it("calls the default agent Talos and every other by its name", () => {
    expect(agentDisplayName("default")).toBe("Talos");
    expect(agentDisplayName("reviewer")).toBe("reviewer");
  });
});

describe("classDisplay", () => {
  const classes = [cls("kourai")];

  it("reads a key as its class's name, and the key itself when unlisted", () => {
    expect(classDisplay(classes, "kourai")).toBe("Kourai");
    expect(classDisplay(classes, "tripodes")).toBe("tripodes");
    expect(classDisplay(undefined, "kourai")).toBe("kourai");
  });
});
