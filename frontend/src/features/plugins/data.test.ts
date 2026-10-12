import { describe, expect, it } from "bun:test";
import {
  filesPath,
  toPlugin,
  toPluginFile,
  toPluginSecret,
  type PluginOut,
} from "./data";

const manifest = {
  description: "Fetches things",
  requirements: ["requests"],
  egress: ["api.example.com"],
  secrets: { TOKEN: "api.example.com" },
  tools: [
    {
      name: "fetch",
      description: "Fetch a URL",
      sensitivity: "external_effect" as const,
      parameters: { type: "object" },
    },
  ],
  agents: ["researcher"],
  skills: ["summarise"],
};

function dto(over: Partial<PluginOut> = {}): PluginOut {
  return {
    id: "p1",
    name: "fetcher",
    source: "agent",
    published: false,
    active: null,
    draft: manifest,
    created_at: "2026-01-02T03:04:05Z",
    updated_at: "2026-01-03T03:04:05Z",
    ...over,
  };
}

describe("toPlugin", () => {
  it("renames the snake_case wire fields and keeps the manifests", () => {
    const p = toPlugin(dto());
    expect(p.createdAt).toBe("2026-01-02T03:04:05Z");
    expect(p.updatedAt).toBe("2026-01-03T03:04:05Z");
    expect(p.active).toBeNull();
    expect(p.draft?.tools[0]).toEqual(manifest.tools[0]);
    expect(p.draft?.secrets).toEqual({ TOKEN: "api.example.com" });
  });

  it("maps agents and skills, defaulting to none for older payloads", () => {
    const p = toPlugin(dto());
    expect(p.draft?.agents).toEqual(["researcher"]);
    expect(p.draft?.skills).toEqual(["summarise"]);
    const { agents: _a, skills: _s, ...old } = manifest;
    const legacy = toPlugin(dto({ draft: old }));
    expect(legacy.draft?.agents).toEqual([]);
    expect(legacy.draft?.skills).toEqual([]);
  });

  it("describes the plugin by its draft, else its active version", () => {
    const active = { ...manifest, description: "Published text" };
    expect(toPlugin(dto({ active, draft: manifest })).description).toBe(
      "Fetches things",
    );
    expect(toPlugin(dto({ active, draft: null })).description).toBe(
      "Published text",
    );
    expect(toPlugin(dto({ active: null, draft: null })).description).toBe("");
  });
});

describe("toPluginFile / toPluginSecret", () => {
  it("maps a binary file's null content and size", () => {
    expect(
      toPluginFile({ path: "a.bin", content: null, size_bytes: 9 }),
    ).toEqual({ path: "a.bin", content: null, sizeBytes: 9 });
  });

  it("maps a secret's set state", () => {
    expect(
      toPluginSecret({ name: "T", domain: "a.com", is_set: true }),
    ).toEqual({ name: "T", domain: "a.com", isSet: true });
  });
});

describe("filesPath", () => {
  it("encodes the id and names the version", () => {
    expect(filesPath("a/b", "draft")).toBe(
      "/plugins/a%2Fb/files?version=draft",
    );
  });
});
