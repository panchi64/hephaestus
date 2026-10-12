/**
 * What the backend actually sends — the snake_case shapes, and nothing that interprets
 * them.
 *
 * Kept apart from the mappers for one reason: these declarations change when the
 * *backend* changes, and the mappers change when the *screen* needs something different
 * from the same payload. A file holding both would be edited from two directions at once,
 * and a reader trying to answer "what does the wire look like" would have to read past the
 * translation to find out.
 *
 * Nothing here is exported past `data/` — the seam types in `../model` are what the rest
 * of the app sees, which is what lets a field be renamed on the wire without the
 * transcript knowing.
 */

import type { ContextWindow, RunMetrics, SummarySection } from "~/lib/stream";
import type {
  ChatActivity,
  ChatOutcome,
  CommandReach,
  SnapshotDiff,
  SnapshotFile,
  ToolInvocation,
} from "../model";

export interface ConversationSummaryDTO {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  preview: string | null;
  /** Two or three sentences on what the agent did here, written by the backend's
   *  utility model once the thread had been idle a while and replaced wholesale each
   *  time it is rewritten. Null until the first sweep has covered the thread.
   *
   *  On the *listing* rather than only the detail because the band that shows it reads
   *  the shared session list, and it is the same class of payload as `preview` beside
   *  it — a capped excerpt, not a document. */
  work_summary?: string | null;
  model: string | null;
  /** The live run's status for this thread (`running`, `queued`,
   *  `awaiting_input`), or null when idle. Registry-derived server-side — the
   *  thread list renders it without opening each conversation. */
  activity?: ChatActivity | null;
  /** How the most recent **terminal** run for this thread ended, or null when the
   *  backend has nothing to say. Null is not "it never ran": the run registry is in
   *  memory and bounded, so a restart or enough traffic drops the answer. Sibling of
   *  `activity` above, not a replacement — both can be set at once, and `activity` is
   *  the one that wins while it is. */
  last_outcome?: ChatOutcome | null;
  /** Where the thread works (`sandbox`, `checkout` or `worktree`), the basename of the
   *  directory a checkout or worktree thread works in, and the project the thread is scoped to. All on the
   *  *listing* because the rail files threads under their project. The id is what it
   *  files *by* — the rail lists every directory, not only the ones holding threads, and
   *  two of them can share a basename. */
  workspace_kind?: string | null;
  workspace?: string | null;
  project_id?: string | null;
}

/** One image on the wire. The REST detail and the SSE `tool.completed` event carry the
 *  identical shape — deliberately, so `toolImages` is one mapping rather than two that
 *  could drift into disagreeing about the same screenshot. */
export interface ToolImageDTO {
  media_type: string;
  data: string;
}

/** One question an `ask_user` call asked, and what the operator said to it — the cold
 *  twin of the live `question.answered` items, paired by the backend from the same
 *  parked arguments so the card reads the same either way. */
export interface ToolCallAnswerDTO {
  question: string;
  selections?: string[];
  text?: string | null;
}

export interface ToolCallDTO {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolInvocation["status"];
  result?: unknown;
  error?: string | null;
  images?: ToolImageDTO[];
  /** Non-empty only on a settled `ask_user` call. */
  answers?: ToolCallAnswerDTO[];
  /** The `run_code` call this one was made from, when a script made it. */
  parent_tool_call_id?: string | null;
}

/** An inline View chip re-attached to the message that minted it — references the
 *  conversation-scoped version by id (the panel reads its bytes/files). */
export interface MessageVersionRefDTO {
  snapshot_id: string;
  title: string | null;
  preview_kind: "html" | "image" | "text" | "other" | null;
  /** The `show` call that minted it — where a reload places the chip. */
  tool_call_id?: string | null;
}

export interface ViewSnapshotDTO {
  snapshot_id: string;
  title: string | null;
  created_at: string;
  files_changed: number;
  summary: string;
  preview_kind: "html" | "image" | "text" | "other" | null;
  preview_artifact_id: string | null;
  /** The operator's durable bookmark on this version. Optional on the wire —
   *  older/mocked payloads may omit it. */
  keeper?: boolean;
}

export interface MessageDTO {
  id: string;
  /** "compaction" is a chassis-authored divider, not a turn — the summary the
   *  thread's earlier turns were folded into, carried in `content`. "subagent" is a
   *  report from one the agent launched, which arrived as a request message and is not
   *  the operator speaking. */
  role: "user" | "assistant" | "compaction" | "subagent";
  content: string;
  tools: ToolCallDTO[];
  versions?: MessageVersionRefDTO[];
  created_at?: string | null;
  /** The model that produced this assistant turn. */
  model?: string | null;
  /** 0-based index of this turn among its sibling versions. */
  version_index?: number;
  /** Total sibling versions for this turn (≥1). */
  version_count?: number;
  /** Whether the operator has pinned this turn. */
  pinned?: boolean;
  /** User turns: ids of the uploads attached to this message. */
  attachment_ids?: string[];
  /** User turns: workspace-relative paths named with `@`. */
  file_refs?: string[];
  /** Set when the run behind this assistant turn ended blocked (a usage/loop/
   *  context/time bound) — the human-readable reason. */
  blocked_reason?: string | null;
  /** Compaction rows: how many **messages** the summary stands in for (not turns —
   *  the backend counts `ModelMessage`s), an estimate of what was folded, and an
   *  estimate of the summary that replaced it. The backend sends all three as ints
   *  (0 when there's nothing to report); optional here only so an older backend
   *  still renders. Every other row carries 0/0/0. */
  messages_compacted?: number | null;
  tokens_before?: number | null;
  tokens_after?: number | null;
  /** Compaction rows: what triggered the fold, matching the live
   *  `conversation.compacted` event's `reason`. Null on every other row, and on a
   *  checkpoint folded before the backend recorded the reason. */
  compaction_reason?: string | null;
  /** Compaction rows: the summary split into the sections the divider renders, parsed
   *  by the backend with the same function the live event uses — so a reload draws the
   *  divider the operator watched arrive. Empty on every other row, and on a checkpoint
   *  whose text parses into nothing (the divider falls back to `content`). */
  sections?: SummarySection[] | null;
  /** Assistant turns: reasoning, calls and answer in the order the model emitted
   *  them — the order `tools`/`content` flatten away, and the only place a turn's
   *  reasoning arrives. A `tool` segment names its entry in `tools`. Empty elsewhere. */
  segments?: MessageSegmentDTO[] | null;
}

export type MessageSegmentDTO =
  | { kind: "thinking" | "text"; text: string; tool_call_id?: null }
  | { kind: "tool"; text?: string; tool_call_id: string };

export interface ActiveRunDTO {
  id: string;
  kind: string;
  status: string;
  last_seq: number;
  /** The persisted row the run is answering — the operator's prompt, or on a
   *  regenerate the request being re-answered. Null for a run that answers nothing
   *  (a fold), and briefly while a chat run is still queued or in its setup. */
  prompt_message_id?: string | null;
}

/** The metric fields shared by the live `run.metrics` frame and the conversation
 *  load's `stats` — the same model server-side, so this is the event type minus its
 *  stream envelope rather than a second declaration that could drift from it. */
export type RunMetricsDTO = Omit<RunMetrics, "type" | "seq" | "ts">;

export interface ConversationDetailDTO extends ConversationSummaryDTO {
  messages: MessageDTO[];
  /** Context-window state reconstructed from the last turn's usage; null when
   *  unavailable. Seeds the meter so an existing thread shows fullness on load. */
  context: ContextWindow | null;
  /** The thread's cumulative readout, rebuilt from the stored messages; null for a
   *  thread that has never run. Seeds the line under the composer on load. */
  stats?: RunMetricsDTO | null;
  /** The in-flight run driving this thread, if a turn is still streaming
   *  server-side; absent/null otherwise. Lets a cold read reattach to it. */
  active_run?: ActiveRunDTO | null;
  /** Workspace snapshots captured across the thread (newest last). Conversation-
   *  scoped — not folded onto a message — so the viewport seeds them separately. */
  snapshots?: ViewSnapshotDTO[];
  /** How far the model may go in this thread. Always populated by the backend, which
   *  resolves it through its own registry — so a thread that predates the column
   *  arrives as the level it was effectively running at rather than as nothing. */
  permission_level?: string | null;
}

/**
 * Shape of a command tool's result; mirrors the tool's return dict.
 *
 * One interface for both tools that run something, because both now answer the same
 * way: the sandboxed host command and the worktree shell each hand back the streams
 * and the exit code as fields. Every key is optional here and none is nullable-by-
 * convention — the backend **omits** a key that has nothing to say rather than sending
 * a null, so presence is the fact and `exit_code` is the one genuine null (a command
 * that timed out never exited).
 */
export interface HostResult {
  ok?: boolean;
  exit_code?: number | null;
  stdout?: string;
  stderr?: string;
  timed_out?: boolean;
  /** Wall clock around the spawn, in whole milliseconds. */
  duration_ms?: number;
  /** What the call declared, and whether an OS fence built from that declaration was
   *  actually applied. Carried at every permission level, not only where a review ran
   *  — which is why the terminal reads them off the result rather than off a review. */
  reach?: CommandReach;
  fenced?: boolean;
  /** Present only when `fenced` is false: one operator-facing sentence saying why. */
  unfenced_reason?: string;
  /** Present only when the output shows a permission failure the fence itself caused,
   *  so a denied write reads as the fence holding rather than as the command being
   *  broken. */
  fence_note?: string;
  error?: string;
}

export interface SnapshotFileDTO {
  path: string;
  status: SnapshotFile["status"];
}

export interface SnapshotDiffDTO {
  path: string;
  status: SnapshotDiff["status"];
  diff: string;
}

export interface OrphanImageAttachmentsDTO {
  upload_ids: string[];
}

export interface ApprovalGrantDTO {
  tool_name: string;
  /** The command the grant covers, as the leading words it was read as (`["uv", "run",
   *  "pytest"]`); empty when it covers the whole tool. The words rather than a joined
   *  string, because this is also what identifies the grant to the revoke endpoint and it
   *  has to round-trip exactly — joining is for the label, which is this layer's job. */
  command_prefix: string[];
  /** Whether the operator picked the wider width — the whole tool, everything it runs.
   *  Sent because the empty prefix above cannot say so on its own. */
  decisive: boolean;
  /** Whether this tool's grants are scoped to a command at all. Only there is "any
   *  command" a real contrast worth putting on the chip. */
  command_scoped: boolean;
  // The backend also returns `expires_at`; the strip shows only the scope, so it's
  // intentionally not mapped into the seam type.
}

/** What settling a park did, beyond resuming the run. Only the standing-yes half is read
 *  here: a "for this conversation" opt-in on a command the backend can scope no grant to
 *  records nothing, and `unscoped` is how that arrives — the approval itself stood. */
export interface ApprovalOutcomeDTO {
  status: string;
  /** The scopes recorded, each as its words; `[]` for a whole-tool grant. */
  granted: string[][];
  /** The tool call ids whose conversation opt-in could not be scoped. */
  unscoped: string[];
}

export interface ChatCreatedDTO {
  run_id: string;
  conversation_id: string;
  /** Set when the send was queued into the conversation's already-live run
   *  (mid-run steering) instead of starting a new one. */
  queued_message_id?: string | null;
}
