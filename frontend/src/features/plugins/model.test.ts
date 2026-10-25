import { describe, expect, it } from "bun:test";
import {
  accessSummary,
  canPublish,
  contributionParts,
  currentManifest,
  hasChanges,
  pluginState,
  publishChanges,
  secretRows,
  toolCount,
  widensAccess,
  type Plugin,
  type PluginManifest,
  type PluginTool,
} from "./model";

function tool(name: string, over: Partial<PluginTool> = {}): PluginTool {
  return {
    name,
    description: "",
    sensitivity: "read",
    parameters: {},
    ...over,
  };
}

function manifest(over: Partial<PluginManifest> = {}): PluginManifest {
  return {
    description: "Fetches things",
    requirements: [],
    egress: [],
    secrets: {},
    tools: [],
    agents: [],
    skills: [],
    routines: [],
    windows: [],
    ...over,
  };
}

function plugin(over: Partial<Plugin> = {}): Plugin {
  return {
    id: "p1",
    name: "fetcher",
    description: "Fetches things",
    source: "user",
    published: false,
    active: null,
    draft: null,
    createdAt: "",
    updatedAt: "",
    ...over,
  };
}

describe("pluginState", () => {
  it("separates published, disabled and never-published", () => {
    expect(pluginState(plugin({ published: true, active: manifest() }))).toBe(
      "published",
    );
    expect(pluginState(plugin({ active: manifest() }))).toBe("disabled");
    expect(pluginState(plugin({ draft: manifest() }))).toBe("draft");
  });
});

describe("currentManifest, toolCount, canPublish", () => {
  it("prefers the draft", () => {
    const p = plugin({
      active: manifest({ tools: [tool("a")] }),
      draft: manifest({ tools: [tool("a"), tool("b")] }),
    });
    expect(currentManifest(p)).toBe(p.draft);
    expect(toolCount(p)).toBe(2);
    expect(toolCount(plugin())).toBe(0);
  });

  it("offers publish for a draft or a disabled plugin only", () => {
    expect(canPublish(plugin({ draft: manifest() }))).toBe(true);
    expect(canPublish(plugin({ active: manifest() }))).toBe(true);
    expect(canPublish(plugin({ published: true, active: manifest() }))).toBe(
      false,
    );
  });
});

describe("contributionParts", () => {
  it("lists tools, agents and skills, leaving out zero counts", () => {
    const p = plugin({
      draft: manifest({
        tools: [tool("a")],
        agents: ["x", "y"],
        skills: ["s"],
        routines: ["r", "q"],
      }),
    });
    expect(contributionParts(p)).toEqual([
      "1 tool",
      "2 agents",
      "1 skill",
      "2 routines",
    ]);
    expect(
      contributionParts(plugin({ draft: manifest({ skills: ["s"] }) })),
    ).toEqual(["1 skill"]);
  });

  it("falls back to zero tools when nothing is contributed", () => {
    expect(contributionParts(plugin())).toEqual(["0 tools"]);
    expect(contributionParts(plugin({ draft: manifest() }))).toEqual([
      "0 tools",
    ]);
  });
});

describe("publishChanges", () => {
  it("treats everything as new on a first publish", () => {
    const c = publishChanges(
      null,
      manifest({
        egress: ["api.example.com"],
        secrets: { TOKEN: "api.example.com" },
        tools: [tool("fetch")],
      }),
    );
    expect(c.egress.added).toEqual(["api.example.com"]);
    expect(c.secrets.added).toEqual([
      { name: "TOKEN", host: "api.example.com" },
    ]);
    expect(c.tools.added.map((t) => t.name)).toEqual(["fetch"]);
    expect(c.descriptionChanged).toBe(false);
    expect(widensAccess(c)).toBe(true);
  });

  it("reports added and removed hosts, secrets and tools", () => {
    const c = publishChanges(
      manifest({
        egress: ["a.com", "b.com"],
        secrets: { OLD: "a.com" },
        tools: [tool("x"), tool("y")],
      }),
      manifest({
        egress: ["b.com", "c.com"],
        secrets: { NEW: "c.com" },
        tools: [tool("y"), tool("z")],
      }),
    );
    expect(c.egress).toEqual({ added: ["c.com"], removed: ["a.com"] });
    expect(c.secrets.added).toEqual([{ name: "NEW", host: "c.com" }]);
    expect(c.secrets.removed).toEqual(["OLD"]);
    expect(c.tools.added.map((t) => t.name)).toEqual(["z"]);
    expect(c.tools.removed).toEqual(["x"]);
  });

  it("flags a secret pointed at another host", () => {
    const c = publishChanges(
      manifest({ secrets: { TOKEN: "a.com" } }),
      manifest({ secrets: { TOKEN: "evil.com" } }),
    );
    expect(c.secrets.rehosted).toEqual([
      { name: "TOKEN", from: "a.com", to: "evil.com" },
    ]);
    expect(widensAccess(c)).toBe(true);
  });

  it("marks only a more sensitive tool as an escalation", () => {
    const up = publishChanges(
      manifest({ tools: [tool("t")] }),
      manifest({ tools: [tool("t", { sensitivity: "external_effect" })] }),
    );
    expect(up.tools.sensitivity).toEqual([
      { name: "t", from: "read", to: "external_effect", escalated: true },
    ]);
    expect(widensAccess(up)).toBe(true);

    const down = publishChanges(
      manifest({ tools: [tool("t", { sensitivity: "external_effect" })] }),
      manifest({ tools: [tool("t")] }),
    );
    expect(down.tools.sensitivity[0].escalated).toBe(false);
    expect(widensAccess(down)).toBe(false);
    expect(hasChanges(down)).toBe(true);
  });

  it("reports nothing for an identical draft", () => {
    const m = manifest({ egress: ["a.com"], tools: [tool("t")] });
    const c = publishChanges(m, m);
    expect(hasChanges(c)).toBe(false);
    expect(widensAccess(c)).toBe(false);
  });

  it("counts a changed description and requirements as changes, not widening", () => {
    const c = publishChanges(
      manifest({ requirements: ["requests"] }),
      manifest({ description: "New", requirements: ["httpx"] }),
    );
    expect(c.descriptionChanged).toBe(true);
    expect(c.requirements).toEqual({ added: ["httpx"], removed: ["requests"] });
    expect(hasChanges(c)).toBe(true);
    expect(widensAccess(c)).toBe(false);
  });
});

describe("agents and skills", () => {
  it("diffs agents and skills and treats additions as widening", () => {
    const c = publishChanges(
      manifest({ agents: ["old"], skills: ["a", "b"] }),
      manifest({ agents: ["new"], skills: ["b"] }),
    );
    expect(c.agents).toEqual({ added: ["new"], removed: ["old"] });
    expect(c.skills).toEqual({ added: [], removed: ["a"] });
    expect(hasChanges(c)).toBe(true);
    expect(widensAccess(c)).toBe(true);
  });

  it("does not widen when only removed", () => {
    const c = publishChanges(
      manifest({ agents: ["a"], skills: ["s"] }),
      manifest(),
    );
    expect(hasChanges(c)).toBe(true);
    expect(widensAccess(c)).toBe(false);
  });

  it("treats a tool-less first publish of a skill as new and widening", () => {
    const c = publishChanges(null, manifest({ skills: ["s"] }));
    expect(c.skills.added).toEqual(["s"]);
    expect(c.tools.added).toEqual([]);
    expect(widensAccess(c)).toBe(true);
  });
});

describe("routines", () => {
  it("diffs routines and treats additions as widening", () => {
    const c = publishChanges(
      manifest({ routines: ["old"] }),
      manifest({ routines: ["new"] }),
    );
    expect(c.routines).toEqual({ added: ["new"], removed: ["old"] });
    expect(hasChanges(c)).toBe(true);
    expect(widensAccess(c)).toBe(true);
  });

  it("does not widen when only removed", () => {
    const c = publishChanges(manifest({ routines: ["r"] }), manifest());
    expect(hasChanges(c)).toBe(true);
    expect(widensAccess(c)).toBe(false);
  });

  it("lists contributed routines in the access summary", () => {
    expect(accessSummary(manifest({ routines: ["a", "b"] }))).toEqual([
      "Routines: a, b",
    ]);
  });
});

describe("accessSummary", () => {
  it("lists contributed agents and skills", () => {
    expect(
      accessSummary(manifest({ agents: ["a"], skills: ["s", "t"] })),
    ).toEqual(["Agents: a", "Skills: s, t"]);
  });

  it("lists hosts and secrets with their hosts", () => {
    expect(
      accessSummary(
        manifest({ egress: ["a.com"], secrets: { TOKEN: "a.com" } }),
      ),
    ).toEqual(["Hosts: a.com", "Secrets: TOKEN → a.com"]);
  });

  it("says so when there is nothing to reach", () => {
    expect(accessSummary(manifest())).toEqual([
      "It declares no hosts or secrets.",
    ]);
  });
});

describe("secretRows", () => {
  it("lists every declared secret, set or not", () => {
    const rows = secretRows({ A: "a.com", B: "b.com" }, [
      { name: "A", domain: "a.com", isSet: true },
    ]);
    expect(rows).toEqual([
      {
        name: "A",
        host: "a.com",
        declaredHost: "a.com",
        isSet: true,
        overridden: false,
      },
      {
        name: "B",
        host: "b.com",
        declaredHost: "b.com",
        isSet: false,
        overridden: false,
      },
    ]);
  });

  it("notes a host override and keeps undeclared stored secrets removable", () => {
    const rows = secretRows({ A: "a.com" }, [
      { name: "A", domain: "proxy.a.com", isSet: true },
      { name: "GONE", domain: "x.com", isSet: true },
    ]);
    expect(rows[0].overridden).toBe(true);
    expect(rows[0].host).toBe("proxy.a.com");
    expect(rows[1]).toMatchObject({ name: "GONE", declaredHost: null });
  });
});
