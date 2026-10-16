/**
 * A thread's sub-agents — the cards, and the transcript behind one.
 *
 * Two reads, and only one of them is about sub-agents at all. The list is this feature's
 * own; a sub-agent's transcript is just a conversation's messages, fetched with the same
 * function the main room uses, because a sub-agent *is* a conversation. Its live tail,
 * its approval and its cancel are run routes for the same reason.
 *
 * **A read that failed is not a thread with no sub-agents**, and the difference is the
 * whole reason this returns a result rather than a list. A deployment with the feature
 * switched off genuinely has none, and neither that nor a thread that has never launched
 * one is worth a broken surface. But one dropped request is not evidence of either — read
 * as "none" it would empty a panel of working sub-agents in front of the operator, and
 * stop the poll that would have corrected it, since nothing left in the list is live.
 */

import { api } from "~/lib/api";
import type { TaskItem } from "~/lib/stream/events";

/** One sub-agent, as the panel shows it. */
export interface Subagent {
  id: string;
  /** Its own thread — where the transcript is read from. */
  conversationId: string;
  /** Its run — the live tail, the approval, and cancelling. */
  runId: string;
  /** The name the launching agent gave *this* sub-agent — `helper-function-finder`
   *  rather than `explorer`. It is what the model addresses it by, and what the operator
   *  reads on the card: a thread with three explorers out has three cards that are
   *  otherwise identical. */
  handle: string;
  /** Its four-letter designation (`BRAX`); empty for a row written before callsigns. */
  callsign: string;
  /** Its class's display name (`Tripodes`); empty for the same old rows. */
  agentClass: string;
  /** Which sub-agent off the roster this is — built-in or project-declared. */
  name: string;
  task: string;
  /** The task list it is keeping for itself. Rides the card rather than being fetched
   *  per sub-agent: this list is already re-read while anything is live, and a thread
   *  with four sub-agents out would otherwise open four more polls to say the same
   *  thing. Empty for one that has not written a list. */
  tasks: TaskItem[];
  status: SubagentStatus;
  /** Its report once it has one; while it runs, its latest answer instead.
   *
   *  **The prose alone** — the backend strips any machine-readable block out before
   *  serving it, so a card cannot end in forty lines of JSON. */
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

/** `blocked` is waiting on the operator rather than on the machine, which is the one
 *  state worth putting in front of them. */
export type SubagentStatus =
  "running" | "blocked" | "done" | "failed" | "cancelled";

/** `BRAX · Tripodes` as plain text, or "" when it has no callsign. */
export function designationText(who: {
  callsign?: string;
  agentClass?: string;
}): string {
  if (!who.callsign) return "";
  return who.agentClass ? `${who.callsign} · ${who.agentClass}` : who.callsign;
}

/** Still expected to report. Both count as live, and a blocked one sorts above the rest
 *  because it is the only one that is waiting on a person. */
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
    // `?? dto.name` for a row written before handles existed — its migration backfilled
    // the spec name, and a card with no title is worse than one naming the roster twice.
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

/** One read of the list. `ok` is false when it could not be read at all, which is a
 *  different fact from an empty list and has to stay one — see the note at the top. */
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
