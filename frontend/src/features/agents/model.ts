/** Custom-agent data contracts and the pure rules around them.
 *
 *  An agent is a named bundle of instructions, a tool allow-list, a permission
 *  ceiling and a workspace posture that a thread can run as. The backend owns
 *  every validation rule; the helpers here only shape what the screens send and
 *  show.
 */

import type { LibraryGroup } from "~/features/library";
import type { Status } from "~/ui";

export type AgentSource = "builtin" | "project" | "user" | "agent" | "plugin";
export type PermissionCeiling = "plan" | "manual" | "edit" | "auto" | "yolo";
export type AgentWorkspace = "shared" | "isolated" | "own";

export interface Agent {
  /** The stored id, or `builtin:<name>` / `project:<name>` for declared ones. */
  id: string;
  name: string;
  description: string;
  /** Its class key (`tripodes`), which fixes its role, voice and callsigns. */
  agentClass: string;
  instructions: string;
  endpointId: string | null;
  model: string | null;
  /** Null means every tool. */
  tools: string[] | null;
  denyTools: string[];
  permissionCeiling: PermissionCeiling | null;
  workspace: AgentWorkspace;
  wallClockTimeoutS: number | null;
  canDelegate: boolean;
  maxDepth: number | null;
  delegable: boolean;
  /** On the launch roster now: delegable, published and not shadowed. */
  launchable: boolean;
  /** Tools without which delegating to it is refused. */
  required: string[];
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
  agent_class?: string;
  instructions?: string;
  endpoint_id?: string | null;
  model?: string | null;
  tools?: string[] | null;
  deny_tools?: string[];
  permission_ceiling?: PermissionCeiling | null;
  workspace?: AgentWorkspace;
  wall_clock_timeout_s?: number | null;
  can_delegate?: boolean;
  max_depth?: number | null;
}

/** One class of automaton (`docs/voice.md` §2), as `GET /agents/classes` sends it. */
export interface AgentClass {
  key: string;
  /** Its mythic name (`Kourai`). */
  display: string;
  role: string;
  /** Callsign stems; empty for a class addressed by name or not at all. */
  callsigns: string[];
  /** Whether an agent the operator writes may take it; Talos and the helpers are not. */
  assignable: boolean;
  /** The class of the agent the operator talks to, at the root of every tree. */
  root: boolean;
}

export interface AgentClasses {
  /** In hierarchy order: Talos first. */
  classes: AgentClass[];
  /** The installation's delegation depth, for an agent that sets none. */
  maxDepth: number;
  /** The class a new agent gets when it names none. */
  defaultClass: string;
}

export const DEFAULT_AGENT_NAME = "default";

export function classSpec(
  classes: readonly AgentClass[] | undefined,
  key: string,
): AgentClass | undefined {
  return classes?.find((c) => c.key === key);
}

/** A class key's display name, or the key itself until the catalog has loaded. */
export function classDisplay(
  classes: readonly AgentClass[] | undefined,
  key: string,
): string {
  return classSpec(classes, key)?.display ?? key;
}

/** Lowercase letters, digits, hyphens and underscores. */
export const AGENT_NAME_PATTERN = /^[a-z0-9_-]+$/;

/** Mirrors the backend's description cap, for the live count only. */
export const AGENT_DESCRIPTION_MAX = 400;

/* ── Directory ────────────────────────────────────────────────────────────── */

/** The small tag on a directory row naming where an agent came from. An agent
 *  the operator wrote themselves is the unremarkable case and carries none. */
export function agentSourceTag(source: AgentSource): string | null {
  switch (source) {
    case "builtin":
      return "BUILT-IN";
    case "project":
      return "PROJECT";
    case "agent":
      return "AGENT-DRAFTED";
    case "plugin":
      return "PLUGIN";
    case "user":
      return null;
  }
}

const SOURCE_ORDER: readonly AgentSource[] = [
  "builtin",
  "project",
  "user",
  "agent",
  "plugin",
];

/** Orders by where an agent came from — built-in first, plugins last. */
export function compareAgentsBySource(a: Agent, b: Agent): number {
  return SOURCE_ORDER.indexOf(a.source) - SOURCE_ORDER.indexOf(b.source);
}

/* ── Classes and the delegation tree ──────────────────────────────────────── */

/** Agents under their class, in hierarchy order: Talos (with `default` first) at the
 *  root, then each sub-agent class. Classes with no agent are left out; an agent whose
 *  class the table doesn't know goes last under its raw key. Order within a group is
 *  otherwise the caller's. */
export function agentHierarchy<T extends Pick<Agent, "name" | "agentClass">>(
  agents: readonly T[],
  classes: readonly AgentClass[],
): LibraryGroup<T>[] {
  const known = new Set(classes.map((c) => c.key));
  const groups = classes.map((c) => {
    const members = agents.filter((a) => a.agentClass === c.key);
    return {
      key: c.key,
      label: c.display,
      note: c.root ? "The agent you talk to. It launches the others." : c.role,
      items: c.root
        ? [
            ...members.filter((a) => a.name === DEFAULT_AGENT_NAME),
            ...members.filter((a) => a.name !== DEFAULT_AGENT_NAME),
          ]
        : members,
    };
  });
  const unknown = [
    ...new Set(
      agents.map((a) => a.agentClass).filter((key) => !known.has(key)),
    ),
  ].map((key) => ({
    key,
    label: key,
    note: "Not a class this backend knows.",
    items: agents.filter((a) => a.agentClass === key),
  }));
  return [...groups, ...unknown].filter((g) => g.items.length > 0);
}

/** How a class's agents are addressed: the first few callsign stems with a serial. */
export function callsignSummary(cls: AgentClass | undefined): string {
  if (!cls) return "—";
  if (cls.root) return `None. Addressed as ${cls.display}`;
  if (cls.callsigns.length === 0) return "None. Not addressed";
  return `${cls.callsigns
    .slice(0, 3)
    .map((stem) => `${stem}-nn`)
    .join(" · ")} · …`;
}

/** Who may launch this agent as a sub-agent, and why not when no one can. */
export function launchedBySummary(agent: Agent): string {
  if (agent.launchable) return "Talos, or any agent that can delegate";
  if (!agent.delegable) return "Not launched by other agents";
  if (!agent.published) return "Not launched until published";
  return "Not launched: a same-named agent takes precedence";
}

/** Whether, and how deep, this agent hands work to others. The backend bounds the
 *  agent's own limit by the installation's, so the lower of the two is what holds. */
export function delegationSummary(
  agent: Agent,
  installMaxDepth: number,
): string {
  if (!agent.canDelegate) return "No";
  const depth = Math.min(agent.maxDepth ?? installMaxDepth, installMaxDepth);
  return `Yes · up to ${depth} ${depth === 1 ? "level" : "levels"} below`;
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

/** What the interface calls an agent: the default one is Talos. */
export function agentDisplayName(name: string): string {
  return name === DEFAULT_AGENT_NAME ? "Talos" : name;
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
