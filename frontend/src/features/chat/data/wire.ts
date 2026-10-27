/**
 * The backend's snake_case wire shapes, with no interpretation. Kept apart from the
 * mappers because these change with the backend and the mappers change with the screen.
 *
 * Not exported past `data/`; the rest of the app sees the seam types in `../model`.
 */

import type { ActionTone } from "~/ui";
import type {
  ContentPartWire,
  ContextWindow,
  RunMetrics,
  SummarySection,
} from "~/lib/stream";
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
  /** The conversation this one was opened from, when it is a thread of an agent tree. */
  parent_id?: string | null;
  preview: string | null;
  /** Two or three sentences on what the agent did here, written by the utility model
   *  after the thread went idle and replaced wholesale on each rewrite. Null until the
   *  first sweep covers the thread. On the listing because the band showing it reads the
   *  shared session list. */
  work_summary?: string | null;
  model: string | null;
  /** The live run's status for this thread (`running`, `queued`, `awaiting_input`), or
   *  null when idle. Registry-derived, so the list needs no per-conversation open. */
  activity?: ChatActivity | null;
  /** What an `awaiting_input` thread is parked on, or null. */
  awaiting_kind?: ActionTone | null;
  /** How the most recent terminal run ended, or null when unknown. Null is not "it never
   *  ran": the run registry is in memory and bounded. Both can be set; `activity` wins
   *  while it is. */
  last_outcome?: ChatOutcome | null;
  /** The basename of the project directory the thread is filed under, and its id. The
   *  rail files by id because it lists every directory and two can share a basename. */
  workspace?: string | null;
  project_id?: string | null;
  /** The custom agent the thread runs as; null is the default agent. */
  agent_name?: string | null;
}

/** The REST detail and the SSE `tool.completed` event carry this identical shape, so
 *  `toolImages` is one mapping. */
export interface ToolImageDTO {
  media_type: string;
  data: string;
}

/** A question an `ask_user` call asked and the operator's answer; the cold twin of the
 *  live `question.answered` items. */
export interface ToolCallAnswerDTO {
  question: string;
  selections?: string[];
  text?: string | null;
}

export type ContentPartDTO = ContentPartWire;

export interface ToolCallDTO {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolInvocation["status"];
  result?: unknown;
  /** The result split into typed parts; `result` stays the raw payload. */
  result_parts?: ContentPartDTO[] | null;
  error?: string | null;
  images?: ToolImageDTO[];
  /** Non-empty only on a settled `ask_user` call. */
  answers?: ToolCallAnswerDTO[];
  /** The `run_code` call this one was made from, when a script made it. */
  parent_tool_call_id?: string | null;
  /** The failure was the platform's own fault. Absent from older backends. */
  platform?: boolean;
}

/** An inline View chip re-attached to the message that minted it, by version id. */
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
  /** The operator's durable bookmark on this version. */
  keeper?: boolean;
}

export interface MessageDTO {
  id: string;
  /** "compaction" is a divider, not a turn: the summary of the folded earlier turns, in
   *  `content`. "subagent" is a report from an agent the thread launched, not the
   *  operator speaking. */
  role: "user" | "assistant" | "compaction" | "subagent" | "notice";
  content: string;
  /** User turns: the non-text parts (attachment fallback text, a routine's previous-run
   *  fence) split out of the operator's bubble, which `content` then holds alone. */
  parts?: ContentPartDTO[] | null;
  /** On a `notice` row: its kind, e.g. `open-tasks`. `content` is the notice body. */
  notice?: string | null;
  /** On a `subagent` row: the handle of the agent that sent it, when it is a message
   *  one agent sent another rather than a report. */
  author?: string | null;
  /** On a `subagent` row that is a report: the handle of the agent that reported. */
  reporter?: string | null;
  tools: ToolCallDTO[];
  versions?: MessageVersionRefDTO[];
  created_at?: string | null;
  model?: string | null;
  /** 0-based index among this turn's sibling versions. */
  version_index?: number;
  version_count?: number;
  pinned?: boolean;
  /** User turns: ids of the uploads attached to this message. */
  attachment_ids?: string[];
  /** User turns: workspace-relative paths named with `@`. */
  file_refs?: string[];
  /** The human-readable reason the run behind this assistant turn ended blocked (a
   *  usage/loop/context/time bound). */
  blocked_reason?: string | null;
  /** Compaction rows: how many messages (not turns; the backend counts `ModelMessage`s)
   *  the summary stands in for, and estimated token counts before and after. Ints, 0 when
   *  nothing to report, on every other row too. */
  messages_compacted?: number | null;
  tokens_before?: number | null;
  tokens_after?: number | null;
  /** Compaction rows: what triggered the fold, as in the live `conversation.compacted`
   *  event. Null elsewhere, and on checkpoints folded before the reason was recorded. */
  compaction_reason?: string | null;
  /** Compaction rows: the summary split into divider sections by the same parser the
   *  live event uses. Empty elsewhere, or when the text parses into nothing (the divider
   *  then falls back to `content`). */
  sections?: SummarySection[] | null;
  /** Compaction rows: what the fold re-read after the briefing. Empty elsewhere. */
  restored?: string[] | null;
  /** Assistant turns: reasoning, calls and answer in emission order, which `tools` and
   *  `content` flatten away; the only place a turn's reasoning arrives. A `tool` segment
   *  names its entry in `tools`. Empty elsewhere. */
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
  /** The persisted row the run answers: the operator's prompt, or the request being
   *  regenerated. Null for a run that answers nothing (a fold), and briefly while a chat
   *  run is queued or in setup. */
  prompt_message_id?: string | null;
}

/** The metrics shared by the live `run.metrics` frame and the load's `stats`: the event
 *  type minus its envelope. */
export type RunMetricsDTO = Omit<RunMetrics, "type" | "seq" | "ts">;

export interface ConversationDetailDTO extends ConversationSummaryDTO {
  messages: MessageDTO[];
  /** Context-window state rebuilt from the last turn's usage; null when unavailable.
   *  Seeds the meter on load. */
  context: ContextWindow | null;
  /** The thread's cumulative readout rebuilt from stored messages; null for a thread
   *  that has never run. Seeds the line under the composer. */
  stats?: RunMetricsDTO | null;
  /** The in-flight run, if a turn is still streaming; lets a cold read reattach. */
  active_run?: ActiveRunDTO | null;
  /** Workspace snapshots across the thread, newest last. Not folded onto a message. */
  snapshots?: ViewSnapshotDTO[];
  /** How far the model may go in this thread. Always populated: the backend resolves
   *  threads that predate the column to their effective level. */
  permission_level?: string | null;
}

/**
 * A command tool's result, for both the sandboxed host command and the worktree shell.
 * The backend omits a key with nothing to say rather than sending null, so presence is
 * the fact; `exit_code` is the one genuine null (a timed-out command never exited).
 */
export interface HostResult {
  ok?: boolean;
  exit_code?: number | null;
  stdout?: string;
  stderr?: string;
  timed_out?: boolean;
  /** Wall clock around the spawn, in whole milliseconds. */
  duration_ms?: number;
  /** What the call declared, and whether an OS fence built from it was applied. Carried
   *  at every permission level, so the terminal reads them here rather than off a review. */
  reach?: CommandReach;
  fenced?: boolean;
  /** Whether a VM command ran with the network; absent for a host command. */
  network?: boolean;
  /** Present only when `fenced` is false: one operator-facing sentence saying why. */
  unfenced_reason?: string;
  /** Present only when the output shows a permission failure the fence caused. */
  fence_note?: string;
  /** Present only when a VM command's egress was refused and the gateway named the host. */
  denied_host?: string;
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

export interface ApprovalGrantDTO {
  tool_name: string;
  /** The command the grant covers as its leading words (`["uv", "run", "pytest"]`);
   *  empty when it covers the whole tool. Kept as words because it also identifies the
   *  grant to the revoke endpoint and must round-trip exactly. */
  command_prefix: string[];
  /** Whether the operator picked the whole tool; the empty prefix cannot say so alone. */
  decisive: boolean;
  /** Whether this tool's grants are scoped to a command at all. */
  command_scoped: boolean;
  // The backend also returns `expires_at`; the strip shows only the scope, so it is not
  // mapped into the seam type.
}

/** What settling a park did beyond resuming the run. Only the standing-yes half is read:
 *  a "for this conversation" opt-in on a command with no scopable grant records nothing,
 *  arrives as `unscoped`, and the approval itself stood. */
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
}

/** A send on a busy conversation: the backend queued it in the inbox rather than
 *  starting a turn. `run_id` is the live run, or null when none is running. */
export interface ChatQueuedDTO {
  conversation_id: string;
  queued_message_id: string;
  run_id: string | null;
}

export type ChatSendDTO = ChatCreatedDTO | ChatQueuedDTO;

export type InboxSourceDTO = "operator" | "subagent" | "agent" | "system";

export interface InboxMessageDTO {
  id: string;
  text: string;
  source: InboxSourceDTO;
  sender: string | null;
  held: boolean;
  turn: boolean;
  notice: string | null;
  queued_at: string;
}

export interface InboxDTO {
  messages: InboxMessageDTO[];
  waiting_for_operator: boolean;
  agents_paused: boolean;
  /** The run on the thread, if one is live. */
  run_id: string | null;
}
