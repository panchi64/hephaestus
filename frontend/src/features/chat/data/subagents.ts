/**
 * A thread's sub-agents: the cards, and the transcript behind one.
 *
 * Only the list is this feature's own. A sub-agent is a conversation, so its transcript
 * uses the main room's message fetch, and its live tail, approval and cancel are run routes.
 *
 * A failed read is not a thread with no sub-agents, hence the `ok` flag. Read as "none"
 * it would empty a panel of working sub-agents and stop the poll that would correct it,
 * since nothing left in the list is live.
 */

import { api } from "~/lib/api";
import type { TaskItem } from "~/lib/stream/events";

/** One sub-agent, as the panel shows it. */
export interface Subagent {
  id: string;
  /** Where the transcript is read from. */
  conversationId: string;
  /** For the live tail, the approval, and cancelling. */
  runId: string;
  /** The name the launching agent gave this sub-agent (`helper-function-finder`, not
   *  `explorer`). The model addresses it by this, and it tells identical cards apart. */
  handle: string;
  /** Its designation (`BRAX-07`); empty for a row written before callsigns. */
  callsign: string;
  /** Its class's display name (`Tripodes`); empty for the same old rows. */
  agentClass: string;
  /** Which sub-agent off the roster this is — built-in or project-declared. */
  name: string;
  task: string;
  /** Its own task list. Rides the list read, which is already polled while anything is
   *  live, instead of a poll per sub-agent. */
  tasks: TaskItem[];
  status: SubagentStatus;
  /** Its report once it has one; while it runs, its latest answer. Prose only: the
   *  backend strips any machine-readable block. */
  summary: string | null;
  error: string | null;
  contextUsed: number | null;
  contextWindow: number | null;
  startedAt: string;
  endedAt: string | null;
  /** The thread this agent was spawned from: the viewed conversation, or another
   *  card's `conversationId`. */
  parentConversationId: string;
  /** 1 for an agent spawned directly by the tree's root thread. */
  depth: number;
  /** Whether the operator has opened it as a thread of its own. */
  opened: boolean;
}

/** `blocked` is waiting on the operator. `stopped` is an agent a time or turn limit
 *  ended, with partial work its parent may continue. */
export type SubagentStatus =
  "running" | "blocked" | "done" | "failed" | "cancelled" | "stopped";

/** `BRAX · Tripodes` as plain text, or "" when it has no callsign. */
export function designationText(who: {
  callsign?: string;
  agentClass?: string;
}): string {
  if (!who.callsign) return "";
  return who.agentClass ? `${who.callsign} · ${who.agentClass}` : who.callsign;
}

/** Still expected to report. */
export function isLive(subagent: Subagent): boolean {
  return subagent.status === "running" || subagent.status === "blocked";
}

interface SubagentDTO {
  id: string;
  conversation_id: string;
  run_id: string;
  handle: string;
  callsign: string;
  agent_class: string;
  name: string;
  task: string;
  tasks: TaskItem[];
  status: SubagentStatus;
  summary: string | null;
  error: string | null;
  context_used: number | null;
  context_window: number | null;
  started_at: string;
  ended_at: string | null;
  parent_conversation_id: string;
  depth: number;
  opened: boolean;
}

function toSubagent(dto: SubagentDTO): Subagent {
  return {
    id: dto.id,
    conversationId: dto.conversation_id,
    runId: dto.run_id,
    // Rows written before handles existed fall back to the spec name.
    handle: dto.handle || dto.name,
    callsign: dto.callsign ?? "",
    agentClass: dto.agent_class ?? "",
    name: dto.name,
    task: dto.task,
    tasks: dto.tasks ?? [],
    status: dto.status,
    summary: dto.summary,
    error: dto.error,
    contextUsed: dto.context_used,
    contextWindow: dto.context_window,
    startedAt: dto.started_at,
    endedAt: dto.ended_at,
    parentConversationId: dto.parent_conversation_id,
    depth: dto.depth,
    opened: dto.opened,
  };
}

/** `ok` is false when the list could not be read, which differs from an empty list. */
export interface SubagentsRead {
  subagents: Subagent[];
  ok: boolean;
}

/** Every agent below this thread at any depth, newest first. */
export async function fetchSubagents(
  conversationId: string,
): Promise<SubagentsRead> {
  try {
    const res = await api.get<{ subagents: SubagentDTO[] }>(
      `/conversations/${conversationId}/subagents`,
    );
    return { subagents: res.subagents.map(toSubagent), ok: true };
  } catch (err) {
    console.warn("sub-agents unavailable", err);
    return { subagents: [], ok: false };
  }
}

/** What a sub-agent's own thread calls it: its callsign, or its handle for rows written
 *  before callsigns. Read from the parent's roster, the only place it is kept. */
export async function fetchOwnCallsign(
  conversationId: string,
  parentId: string,
): Promise<string | undefined> {
  const read = await fetchSubagents(parentId);
  const own = read.subagents.find((s) => s.conversationId === conversationId);
  return own ? own.callsign || own.handle : undefined;
}

/** Stop every live agent below a thread that has no run of its own. Their reports go
 *  to the held inbox, so nothing wakes the thread. Rejects with a 409 when the thread
 *  has a run: that run's cancel stops its agents. */
export async function stopThreadAgents(
  conversationId: string,
): Promise<string[]> {
  const res = await api.post<{ stopped: string[] }>(
    `/conversations/${conversationId}/agents/stop`,
    {},
  );
  return res.stopped;
}

/** Cancel one agent's run. Resolves false when the backend refused it. */
export async function stopSubagent(runId: string): Promise<boolean> {
  try {
    await api.post(`/runs/${runId}/cancel`, {});
    return true;
  } catch (err) {
    console.warn("sub-agent could not be stopped", err);
    return false;
  }
}

/** Open an agent's conversation as a thread of its own; the id to navigate to, or null
 *  when the backend refused it. */
export async function openSubagentThread(
  conversationId: string,
): Promise<string | null> {
  try {
    const res = await api.post<{ id: string; parent_id: string }>(
      `/conversations/${conversationId}/open`,
      {},
    );
    return res.id;
  } catch (err) {
    console.warn("sub-agent could not be opened as a thread", err);
    return null;
  }
}
