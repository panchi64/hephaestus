/**
 * One conversation: how it is read, and everything the operator can do to it that isn't a
 * turn.
 *
 * Renaming, retitling, forking, deleting, revoking a grant, forcing compaction on or off,
 * raising the thread's browser window — each is a thin relay to a live endpoint, and each
 * ends by telling the list to re-read itself rather than patching a second copy of the
 * truth locally. That is the rule this module exists to keep: the backend owns a
 * conversation's state, and the client's job is to ask again, not to guess what the answer
 * became.
 */

import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type { TaskItem } from "~/lib/stream";
import {
  permissionLevel,
  type ApprovalGrant,
  type ChatSession,
  type CompactionState,
  type PlanDocument,
} from "../model";
import { toMessage } from "./messages";
import { deriveTitle, toActiveRun, toStats } from "./summaries";
import { toViewSnapshotRef } from "./viewSnapshots";
import { refreshSessions } from "./sessions";
import { revealTitle } from "./titleReveals";
import type {
  ApprovalGrantDTO,
  ConversationDetailDTO,
  ConversationSummaryDTO,
  OrphanImageAttachmentsDTO,
} from "./wire";

async function fetchSession(id: string): Promise<ChatSession> {
  const dto = await api.get<ConversationDetailDTO>(`/conversations/${id}`);
  return {
    id: dto.id,
    title: deriveTitle(dto),
    messages: dto.messages.map(toMessage),
    context: dto.context,
    stats: dto.stats ? toStats(dto.stats) : null,
    activeRun: toActiveRun(dto.active_run),
    snapshots: (dto.snapshots ?? []).map(toViewSnapshotRef),
    permission: permissionLevel(dto.permission_level ?? undefined),
  };
}

/** Loads a session. A null id means a new, unsaved conversation — the resource
 *  doesn't fetch, so the screen renders an empty thread.
 *
 *  `revision` re-reads the same thread when it changes. The main room does not need it —
 *  its transcript is fed by the run stream, and a re-read there would fight the stream —
 *  but a thread nobody is streaming has no other way to move: a sub-agent's transcript is
 *  read from outside its own run, and the panel's poll is what knows it has got further. */
export function useChatSession(
  id: () => string | null,
  revision: () => unknown = () => null,
): Resource<ChatSession> {
  const [data] = createResource(
    () => {
      const value = id();
      return value === null ? null : ([value, revision()] as const);
    },
    ([value]) => fetchSession(value),
  );
  return data;
}

export async function renameConversation(
  id: string,
  title: string,
): Promise<void> {
  await api.patch(`/conversations/${id}`, { title });
  refreshSessions();
}

/** Re-derive a thread's title on demand. The backend names it from every question
 *  the operator asked across the thread (not just the opening line, and never the
 *  assistant's replies), then this reveals the result with the same typewriter
 *  animation as the first-turn auto-title so both surfaces stay in lockstep. */
export async function regenerateTitle(id: string): Promise<void> {
  // Titling resolves the backend `utility`→`main` binding (with its pinned model),
  // the same source every background caller uses — no per-request override needed.
  const summary = await api.post<ConversationSummaryDTO>(
    `/conversations/${id}/retitle`,
    {},
  );
  if (summary.title) revealTitle(id, summary.title);
  refreshSessions();
}

export async function deleteConversation(
  id: string,
  purgeImages = false,
  discardBranch = false,
): Promise<void> {
  const params = [
    purgeImages ? "purgeImages=true" : "",
    // A code thread with unmerged commits is refused unless this says so —
    // the backend decides, this only relays the operator's answer.
    discardBranch ? "discardBranch=true" : "",
  ].filter(Boolean);
  const q = params.length ? `?${params.join("&")}` : "";
  await api.del(`/conversations/${id}${q}`);
  refreshSessions();
}

/** Copy a thread's history up to `messageId` into a new conversation and return
 *  its id. The backend returns the *fork's* detail, so the caller navigates in one
 *  round-trip; the source thread is untouched. */
export async function forkConversation(
  conversationId: string,
  messageId: string,
): Promise<string> {
  const detail = await api.post<ConversationDetailDTO>(
    `/conversations/${conversationId}/messages/${messageId}/fork`,
    {},
  );
  refreshSessions();
  return detail.id;
}

/** Image uploads a pending delete would orphan — ones nothing else references.
 *  Probed before a conversation/message delete so the operator can be offered the
 *  keep-or-purge choice. Empty ⇒ nothing would be left unused, delete outright.
 *  Omit `messageId` for a whole-conversation delete. */
export async function fetchOrphanImageAttachments(
  conversationId: string,
  messageId?: string,
): Promise<string[]> {
  const q = messageId ? `?messageId=${encodeURIComponent(messageId)}` : "";
  const res = await api.get<OrphanImageAttachmentsDTO>(
    `/conversations/${conversationId}/orphan-image-attachments${q}`,
  );
  return res.upload_ids;
}

// Bumped when an approval is submitted that records a conversation grant, so the
// AUTO-APPROVED strip refetches at approve time rather than waiting for the next
// stream-state toggle (the run is already mid-flight when the grant is made).
const [grantsRevision, setGrantsRevision] = createSignal(0);
export const conversationGrantsRevision = grantsRevision;

/** Tell the AUTO-APPROVED strip a grant was just recorded, so it refetches now
 *  rather than at the next stream-state toggle. */
export function bumpGrantsRevision(): void {
  setGrantsRevision((n) => n + 1);
}

/** The tools the operator allowed to auto-approve for the rest of this conversation. */
export async function fetchGrants(
  conversationId: string,
): Promise<ApprovalGrant[]> {
  const rows = await api.get<ApprovalGrantDTO[]>(
    `/conversations/${conversationId}/grants`,
  );
  return rows.map((r) => ({
    toolName: r.tool_name,
    commandPrefix: r.command_prefix ?? [],
    decisive: r.decisive ?? false,
    commandScoped: r.command_scoped ?? false,
  }));
}

/** The agent's task list for a thread.
 *
 *  The list also arrives live on `tasks.updated`, but a client opening or reloading a
 *  conversation has no stream to replay — this is how the panel starts from the truth
 *  instead of staying empty until the next mutation.
 */
export async function fetchTasks(conversationId: string): Promise<TaskItem[]> {
  return api.get<TaskItem[]>(`/conversations/${conversationId}/tasks`);
}

/** The written plan a thread is working to, or null where it has none.
 *
 *  The backfill half of `plan.updated`, for the same reason as the list above — and
 *  `null` rather than a 404, because having no plan is the ordinary state of nearly
 *  every thread rather than a miss.
 */
export async function fetchPlan(
  conversationId: string,
): Promise<PlanDocument | null> {
  return api.get<PlanDocument | null>(`/conversations/${conversationId}/plan`);
}

/** Open (or bring forward) this thread's agent browser — a real Chromium window on the
 *  host that the operator and the agent share.
 *
 *  There is nothing to render here: the window *is* the view, so the call returns
 *  nothing and its only visible outcome is the window arriving in front. The backend
 *  answers 503 with a sentence when it could not launch one; the caller surfaces that
 *  detail rather than a generic failure, since "why not" is the whole of what the
 *  operator can act on. */
export async function openBrowser(conversationId: string): Promise<void> {
  await api.post(`/browser/session/${conversationId}`, {});
}

/** Revoke one conversation auto-approval — the next call it covered asks again.
 *
 *  `commandPrefix` says *which* grant on that tool, since a command-running tool can hold
 *  several at once. Each word is sent as its own `command_prefix` parameter — the list the
 *  listing gave, word for word, so what the operator revokes is the grant they were shown
 *  and never one re-derived by splitting a sentence. */
export async function revokeGrant(
  conversationId: string,
  toolName: string,
  commandPrefix: string[] = [],
): Promise<void> {
  const scope = commandPrefix
    .map((word) => `command_prefix=${encodeURIComponent(word)}`)
    .join("&");
  await api.del(
    `/conversations/${conversationId}/grants/${encodeURIComponent(toolName)}${scope ? `?${scope}` : ""}`,
  );
}

/** This conversation's compaction state (its override + the resolved effective on/off) —
 *  folding older turns into a summary once the context window fills. The only reduction
 *  there is; per-tool-result digesting was removed. */
export async function fetchAutoCompactOverride(
  conversationId: string,
): Promise<CompactionState> {
  return api.get<CompactionState>(
    `/conversations/${conversationId}/auto-compact`,
  );
}

/** Force conversation compaction on/off for this chat (or `null` to inherit the global
 *  setting); returns the new state. */
export async function setAutoCompactOverride(
  conversationId: string,
  override: boolean | null,
): Promise<CompactionState> {
  return api.put<CompactionState>(
    `/conversations/${conversationId}/auto-compact`,
    { override },
  );
}
