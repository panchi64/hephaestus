/** Plugin data contracts and the pure rules around them.
 *
 *  A plugin is a set of Python tools that run in the platform VM, and may also
 *  contribute agents and skills. An agent's
 *  work is a draft; the operator reviews it and publishes it, which makes its
 *  tools callable and its agents and skills available. The helpers here shape what the screens show — the backend
 *  owns every validation rule.
 */

import { plural } from "~/lib/format";
import type { Status } from "~/ui";

export type PluginSource = "user" | "agent";
export type Sensitivity = "read" | "workspace_write" | "external_effect";

export interface PluginTool {
  name: string;
  description: string;
  sensitivity: Sensitivity;
  /** JSON Schema of the tool's arguments. */
  parameters: Record<string, unknown>;
}

export interface PluginManifest {
  description: string;
  requirements: string[];
  /** Hosts the plugin may reach. */
  egress: string[];
  /** Secret name → the host it is sent to. */
  secrets: Record<string, string>;
  tools: PluginTool[];
  /** Names of the agents it contributes while published. */
  agents: string[];
  /** Names of the skills it contributes while published. */
  skills: string[];
}

export interface Plugin {
  id: string;
  name: string;
  /** The draft's description, else the active version's. */
  description: string;
  source: PluginSource;
  /** Its tools are callable. */
  published: boolean;
  /** The version whose tools run; null until first published. */
  active: PluginManifest | null;
  /** The working copy awaiting review. */
  draft: PluginManifest | null;
  createdAt: string;
  updatedAt: string;
}

export interface PluginFile {
  path: string;
  /** Null for a binary file. */
  content: string | null;
  sizeBytes: number;
}

export type PluginVersion = "current" | "draft" | "active";

export interface PluginSecret {
  name: string;
  domain: string;
  isSet: boolean;
}

/* ── Directory ────────────────────────────────────────────────────────────── */

export type PluginState = "published" | "disabled" | "draft";

/** Never published is a draft; published once and switched off is disabled. */
export function pluginState(plugin: Plugin): PluginState {
  if (plugin.published) return "published";
  return plugin.active ? "disabled" : "draft";
}

export function pluginStateLabel(state: PluginState): string {
  return state.toUpperCase();
}

export function pluginStateFlag(state: PluginState): Status {
  return state === "published" ? "nominal" : "idle";
}

/** A draft exists that the operator has not yet published or discarded. */
export function hasPendingDraft(plugin: Plugin): boolean {
  return plugin.draft !== null;
}

export function isAgentDrafted(plugin: Plugin): boolean {
  return plugin.source === "agent";
}

/** The manifest the screens describe: the draft when there is one. */
export function currentManifest(plugin: Plugin): PluginManifest | null {
  return plugin.draft ?? plugin.active;
}

export function pluginDescription(
  active: PluginManifest | null,
  draft: PluginManifest | null,
): string {
  return (draft ?? active)?.description ?? "";
}

export function toolCount(plugin: Plugin): number {
  return currentManifest(plugin)?.tools.length ?? 0;
}

/** What the plugin contributes, as short labels: "2 tools", "1 agent". Zero
 *  counts are left out unless nothing is contributed. */
export function contributionParts(plugin: Plugin): string[] {
  const m = currentManifest(plugin);
  const counts: [number, string, string][] = [
    [m?.tools.length ?? 0, "tool", "tools"],
    [m?.agents.length ?? 0, "agent", "agents"],
    [m?.skills.length ?? 0, "skill", "skills"],
  ];
  const parts = counts
    .filter(([n]) => n > 0)
    .map(([n, one, many]) => plural(n, one, many));
  return parts.length ? parts : ["0 tools"];
}

/** What Publish acts on: the draft, or the active version when re-enabling. */
export function publishTarget(plugin: Plugin): PluginManifest | null {
  return plugin.draft ?? plugin.active;
}

/** Publish is offered for a draft, and for a disabled plugin to switch back on. */
export function canPublish(plugin: Plugin): boolean {
  return plugin.draft !== null || (!plugin.published && plugin.active !== null);
}

/* ── Sensitivity ──────────────────────────────────────────────────────────── */

const SENSITIVITY_RANK: Record<Sensitivity, number> = {
  read: 0,
  workspace_write: 1,
  external_effect: 2,
};

export function sensitivityLabel(s: Sensitivity): string {
  switch (s) {
    case "read":
      return "READ";
    case "workspace_write":
      return "WORKSPACE WRITE";
    case "external_effect":
      return "EXTERNAL EFFECT";
  }
}

export function sensitivityFlag(s: Sensitivity): Status {
  switch (s) {
    case "read":
      return "nominal";
    case "workspace_write":
      return "warn";
    case "external_effect":
      return "alert";
  }
}

/* ── What publishing changes ──────────────────────────────────────────────── */

export interface SecretUse {
  name: string;
  host: string;
}

export interface SensitivityChange {
  name: string;
  from: Sensitivity;
  to: Sensitivity;
  /** The tool can now do more than it could. */
  escalated: boolean;
}

export interface PublishChanges {
  egress: { added: string[]; removed: string[] };
  secrets: {
    added: SecretUse[];
    removed: string[];
    /** Same secret, sent to a different host. */
    rehosted: { name: string; from: string; to: string }[];
  };
  tools: {
    added: PluginTool[];
    removed: string[];
    sensitivity: SensitivityChange[];
  };
  requirements: { added: string[]; removed: string[] };
  agents: { added: string[]; removed: string[] };
  skills: { added: string[]; removed: string[] };
  descriptionChanged: boolean;
}

function diffSet(
  before: readonly string[],
  after: readonly string[],
): { added: string[]; removed: string[] } {
  const was = new Set(before);
  const now = new Set(after);
  return {
    added: after.filter((v) => !was.has(v)),
    removed: before.filter((v) => !now.has(v)),
  };
}

/** What the operator approves by publishing `draft` over `active`. With no
 *  active version everything in the draft is new. */
export function publishChanges(
  active: PluginManifest | null,
  draft: PluginManifest,
): PublishChanges {
  const base: PluginManifest = active ?? {
    description: "",
    requirements: [],
    egress: [],
    secrets: {},
    tools: [],
    agents: [],
    skills: [],
  };

  const secretNames = diffSet(
    Object.keys(base.secrets),
    Object.keys(draft.secrets),
  );
  const rehosted = Object.keys(draft.secrets)
    .filter(
      (name) =>
        name in base.secrets && base.secrets[name] !== draft.secrets[name],
    )
    .map((name) => ({
      name,
      from: base.secrets[name],
      to: draft.secrets[name],
    }));

  const before = new Map(base.tools.map((t) => [t.name, t]));
  const names = new Set(draft.tools.map((t) => t.name));
  const sensitivity: SensitivityChange[] = [];
  for (const tool of draft.tools) {
    const prev = before.get(tool.name);
    if (prev && prev.sensitivity !== tool.sensitivity)
      sensitivity.push({
        name: tool.name,
        from: prev.sensitivity,
        to: tool.sensitivity,
        escalated:
          SENSITIVITY_RANK[tool.sensitivity] >
          SENSITIVITY_RANK[prev.sensitivity],
      });
  }

  return {
    egress: diffSet(base.egress, draft.egress),
    secrets: {
      added: secretNames.added.map((name) => ({
        name,
        host: draft.secrets[name],
      })),
      removed: secretNames.removed,
      rehosted,
    },
    tools: {
      added: draft.tools.filter((t) => !before.has(t.name)),
      removed: base.tools.map((t) => t.name).filter((n) => !names.has(n)),
      sensitivity,
    },
    requirements: diffSet(base.requirements, draft.requirements),
    agents: diffSet(base.agents, draft.agents),
    skills: diffSet(base.skills, draft.skills),
    descriptionChanged:
      active !== null && active.description !== draft.description,
  };
}

/** Whether publishing widens what the plugin can reach or do: a new host, a new
 *  or re-pointed secret, a new tool, a new agent or skill (standing instructions the model acts
 *  under), or a tool that became more sensitive. */
export function widensAccess(changes: PublishChanges): boolean {
  return (
    changes.egress.added.length > 0 ||
    changes.secrets.added.length > 0 ||
    changes.secrets.rehosted.length > 0 ||
    changes.tools.added.length > 0 ||
    changes.agents.added.length > 0 ||
    changes.skills.added.length > 0 ||
    changes.tools.sensitivity.some((s) => s.escalated)
  );
}

/** Whether the draft differs from the active version at all. */
export function hasChanges(changes: PublishChanges): boolean {
  return (
    changes.egress.added.length +
      changes.egress.removed.length +
      changes.secrets.added.length +
      changes.secrets.removed.length +
      changes.secrets.rehosted.length +
      changes.tools.added.length +
      changes.tools.removed.length +
      changes.tools.sensitivity.length +
      changes.requirements.added.length +
      changes.requirements.removed.length +
      changes.agents.added.length +
      changes.agents.removed.length +
      changes.skills.added.length +
      changes.skills.removed.length >
      0 || changes.descriptionChanged
  );
}

/** The line a confirm lists for what a manifest will be able to reach. */
export function accessSummary(manifest: PluginManifest): string[] {
  const lines: string[] = [];
  if (manifest.egress.length)
    lines.push(`Hosts: ${manifest.egress.join(", ")}`);
  const secrets = Object.entries(manifest.secrets);
  if (secrets.length)
    lines.push(
      `Secrets: ${secrets.map(([name, host]) => `${name} → ${host}`).join(", ")}`,
    );
  if (manifest.agents.length)
    lines.push(`Agents: ${manifest.agents.join(", ")}`);
  if (manifest.skills.length)
    lines.push(`Skills: ${manifest.skills.join(", ")}`);
  if (!lines.length) lines.push("It declares no hosts or secrets.");
  return lines;
}

/* ── Secrets ──────────────────────────────────────────────────────────────── */

export interface SecretRow {
  name: string;
  /** The host the value is sent to: the stored override, else the declared one. */
  host: string;
  declaredHost: string | null;
  isSet: boolean;
  /** Stored against a host other than the one the manifest declares. */
  overridden: boolean;
}

/** Every declared secret, set or not, followed by stored ones the manifest no
 *  longer declares so they can still be removed. */
export function secretRows(
  declared: Record<string, string>,
  stored: readonly PluginSecret[],
): SecretRow[] {
  const byName = new Map(stored.map((s) => [s.name, s]));
  const rows: SecretRow[] = Object.entries(declared).map(([name, host]) => {
    const row = byName.get(name);
    const effective = row?.domain || host;
    return {
      name,
      host: effective,
      declaredHost: host,
      isSet: row?.isSet ?? false,
      overridden: Boolean(row?.isSet) && effective !== host,
    };
  });
  for (const s of stored)
    if (!(s.name in declared))
      rows.push({
        name: s.name,
        host: s.domain,
        declaredHost: null,
        isSet: s.isSet,
        overridden: false,
      });
  return rows;
}

/** The text shown in place of a binary file. */
export function binaryNote(file: PluginFile): string {
  return `Binary file (${file.sizeBytes} bytes) — not shown.`;
}
