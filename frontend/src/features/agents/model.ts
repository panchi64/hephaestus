/** Custom-agent data contracts and the pure rules around them.
 *
 *  An agent is a named bundle of instructions, a tool allow-list, a permission
 *  ceiling and a workspace posture that a thread can run as. The backend owns
 *  every validation rule; the helpers here only shape what the screens send and
 *  show.
 */

import type { Status } from "~/ui";

export type AgentSource = "builtin" | "project" | "user" | "agent" | "plugin";
export type PermissionCeiling = "plan" | "manual" | "edit" | "auto" | "yolo";
export type AgentWorkspace = "shared" | "isolated" | "own";

export interface Agent {
  /** The stored id, or `builtin:<name>` / `project:<name>` for declared ones. */
  id: string;
  name: string;
  description: string;
  instructions: string;
  endpointId: string | null;
  model: string | null;
  /** Null means every tool. */
  tools: string[] | null;
  denyTools: string[];
  permissionCeiling: PermissionCeiling | null;
  workspace: AgentWorkspace;
  requestLimit: number | null;
  wallClockTimeoutS: number | null;
  canDelegate: boolean;
  maxDepth: number | null;
  delegable: boolean;
  source: AgentSource;
  published: boolean;
  /** False when a same-named agent of higher precedence shadows it, or it is unpublished. */
  active: boolean;
  editable: boolean;
}

/** The body of `POST /agents` and `PATCH /agents/{id}`, snake_case on the wire. */
export interface AgentWrite {
  name?: string;
  description?: string;
  instructions?: string;
  endpoint_id?: string | null;
  model?: string | null;
  tools?: string[] | null;
  deny_tools?: string[];
  permission_ceiling?: PermissionCeiling | null;
  workspace?: AgentWorkspace;
  request_limit?: number | null;
  wall_clock_timeout_s?: number | null;
  can_delegate?: boolean;
  max_depth?: number | null;
}

export const DEFAULT_AGENT_NAME = "default";

/** Lowercase letters, digits, hyphens and underscores. */
export const AGENT_NAME_PATTERN = /^[a-z0-9_-]+$/;

export const PERMISSION_CEILINGS: readonly PermissionCeiling[] = [
  "plan",
  "manual",
  "edit",
  "auto",
  "yolo",
];

export const WORKSPACE_OPTIONS: readonly {
  value: AgentWorkspace;
  label: string;
}[] = [
  { value: "shared", label: "Shared" },
  { value: "isolated", label: "Isolated" },
  { value: "own", label: "Own" },
];

/* ── Directory ────────────────────────────────────────────────────────────── */

const SOURCE_GROUPS: readonly { source: AgentSource; label: string }[] = [
  { source: "builtin", label: "Built-in" },
  { source: "project", label: "Project" },
  { source: "user", label: "Yours" },
  { source: "agent", label: "Drafted by agents" },
  { source: "plugin", label: "Plugins" },
];

export interface AgentGroup {
  source: AgentSource;
  label: string;
  agents: Agent[];
}

/** Agents filed under their source, in a fixed order; empty groups are dropped. */
export function groupAgentsBySource(agents: readonly Agent[]): AgentGroup[] {
  return SOURCE_GROUPS.map(({ source, label }) => ({
    source,
    label,
    agents: agents.filter((a) => a.source === source),
  })).filter((g) => g.agents.length > 0);
}

export type AgentState = "published" | "draft" | "shadowed";

/** A published agent that is not active is shadowed by a higher-precedence one. */
export function agentState(agent: Agent): AgentState {
  if (!agent.published) return "draft";
  return agent.active ? "published" : "shadowed";
}

export function agentStateLabel(state: AgentState): string {
  return state.toUpperCase();
}

/** A draft or a shadowed agent is merely inactive, never a warning. */
export function agentStateFlag(state: AgentState): Status {
  return state === "published" ? "nominal" : "idle";
}

/** An agent-authored draft is waiting on the operator to read and publish it. */
export function isAwaitingPublish(agent: Agent): boolean {
  return agent.source === "agent" && !agent.published;
}

/* ── Composer picker ──────────────────────────────────────────────────────── */

/** `default` first, then every published, active agent by name. */
export function pickableAgents(agents: readonly Agent[]): Agent[] {
  const live = agents
    .filter((a) => a.published && a.active && a.name !== DEFAULT_AGENT_NAME)
    .sort((a, b) => a.name.localeCompare(b.name));
  const base = agents.find((a) => a.name === DEFAULT_AGENT_NAME);
  return base ? [base, ...live] : live;
}

/** The thread's agent name as stored: null means the default. */
export function threadAgentName(stored: string | null | undefined): string {
  return stored ?? DEFAULT_AGENT_NAME;
}

/**
 * What goes in the `agent` field of a chat send, if anything. Nothing unless the
 * operator picked one that has not been sent yet; a new thread also omits the
 * default, which is what an omitted field already means.
 */
export function agentToSend(args: {
  choice: string | undefined;
  sent: string | undefined;
  isNewThread: boolean;
}): string | undefined {
  const { choice, sent, isNewThread } = args;
  if (choice === undefined || choice === sent) return undefined;
  if (isNewThread && choice === DEFAULT_AGENT_NAME) return undefined;
  return choice;
}

/* ── Editor ───────────────────────────────────────────────────────────────── */

/** The editor's working copy. Lists are comma text and numbers are text, so a
 *  half-typed value is never coerced under the operator's cursor. */
export interface AgentDraft {
  name: string;
  description: string;
  instructions: string;
  endpointId: string | null;
  model: string | null;
  allTools: boolean;
  toolsText: string;
  denyText: string;
  permissionCeiling: PermissionCeiling | null;
  workspace: AgentWorkspace;
  requestLimit: string;
  wallClockTimeoutS: string;
  canDelegate: boolean;
  maxDepth: string;
}

const WRITE_KEYS: readonly (keyof AgentWrite)[] = [
  "name",
  "description",
  "instructions",
  "endpoint_id",
  "model",
  "tools",
  "deny_tools",
  "permission_ceiling",
  "workspace",
  "request_limit",
  "wall_clock_timeout_s",
  "can_delegate",
  "max_depth",
];

export function emptyDraft(): AgentDraft {
  return {
    name: "",
    description: "",
    instructions: "",
    endpointId: null,
    model: null,
    allTools: true,
    toolsText: "",
    denyText: "",
    permissionCeiling: null,
    workspace: "shared",
    requestLimit: "",
    wallClockTimeoutS: "",
    canDelegate: false,
    maxDepth: "",
  };
}

export function draftOf(agent: Agent): AgentDraft {
  return {
    name: agent.name,
    description: agent.description,
    instructions: agent.instructions,
    endpointId: agent.endpointId,
    model: agent.model,
    allTools: agent.tools === null,
    toolsText: formatList(agent.tools ?? []),
    denyText: formatList(agent.denyTools),
    permissionCeiling: agent.permissionCeiling,
    workspace: agent.workspace,
    requestLimit: numberText(agent.requestLimit),
    wallClockTimeoutS: numberText(agent.wallClockTimeoutS),
    canDelegate: agent.canDelegate,
    maxDepth: numberText(agent.maxDepth),
  };
}

function numberText(value: number | null): string {
  return value === null ? "" : String(value);
}

/** `"a, b ,,c"` → `["a","b","c"]`, dropping blanks and repeats. */
export function parseList(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[,\n]/)) {
    const item = part.trim();
    if (item) seen.add(item);
  }
  return [...seen];
}

export function formatList(items: readonly string[]): string {
  return items.join(", ");
}

/** Blank clears the limit (null); text that is not a number is left out
 *  (undefined) so the backend never receives a coerced guess. */
export function parseLimit(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : undefined;
}

/** The draft as a complete `AgentWrite`. */
export function draftToWrite(draft: AgentDraft): AgentWrite {
  const write: AgentWrite = {
    name: draft.name.trim(),
    description: draft.description.trim(),
    instructions: draft.instructions,
    endpoint_id: draft.endpointId,
    model: draft.model,
    tools: draft.allTools ? null : parseList(draft.toolsText),
    deny_tools: parseList(draft.denyText),
    permission_ceiling: draft.permissionCeiling,
    workspace: draft.workspace,
    can_delegate: draft.canDelegate,
  };
  const requestLimit = parseLimit(draft.requestLimit);
  if (requestLimit !== undefined) write.request_limit = requestLimit;
  const timeout = parseLimit(draft.wallClockTimeoutS);
  if (timeout !== undefined) write.wall_clock_timeout_s = timeout;
  const maxDepth = parseLimit(draft.maxDepth);
  if (maxDepth !== undefined) write.max_depth = maxDepth;
  return write;
}

/**
 * The fields of `draft` that differ from the stored `agent`, for a PATCH. A
 * cleared field is sent as an explicit null; an unchanged one is not sent.
 */
export function buildAgentPatch(draft: AgentDraft, agent: Agent): AgentWrite {
  const next = draftToWrite(draft);
  const prev = draftToWrite(draftOf(agent));
  const patch: AgentWrite = {};
  function carry<K extends keyof AgentWrite>(key: K): void {
    if (JSON.stringify(next[key]) !== JSON.stringify(prev[key])) {
      patch[key] = next[key];
    }
  }
  for (const key of WRITE_KEYS) carry(key);
  return patch;
}

export function isDraftDirty(draft: AgentDraft, agent: Agent): boolean {
  return Object.keys(buildAgentPatch(draft, agent)).length > 0;
}
