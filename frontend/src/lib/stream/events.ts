/**
 * The run event protocol: the client mirror of `backend/runs/events/` (v1). Every frame
 * on a run's SSE stream is a flat envelope `{ type, seq, ts, ...payload }`. Keep it in
 * lockstep with the backend union.
 */

export const PROTOCOL_VERSION = 1;

interface Base {
  seq: number;
  ts: string;
}

// --- Run lifecycle ---------------------------------------------------------
/** The run is waiting for its model endpoint to have room; re-sent whenever its place
 *  in line moves. `position` counts from 1. `run.started` ends the wait. */
export interface RunQueued extends Base {
  type: "run.queued";
  position: number;
  endpoint: string;
}
export interface RunStarted extends Base {
  type: "run.started";
  run_id: string;
  kind: string;
  protocol_version: number;
}
/** What the occupied part of the window is holding, three ways. The backend scales them
 *  to sum to `ContextWindow.used`. Every figure is an estimate: no provider reports a
 *  breakdown, so the split is measured backend-side and scaled to the provider's total.
 *  Render them with a `~`. */
export interface ContextComposition {
  /** The standing brief: instructions + system prompt. */
  system: number;
  /** Every tool name, description and JSON schema handed to the model. */
  tools: number;
  /** The conversation itself. */
  messages: number;
  /** The same tokens itemised. Each segment belongs to one group and a group's segments
   *  sum to its total above. Empty when the backend measured the totals but not the
   *  detail. */
  segments: ContextSegment[];
}

/** One line item inside a group. The backend emits no zero rows and no fixed roster, so
 *  render whatever arrives and never expect a particular id. */
export interface ContextSegment {
  /** A backend slug: a tool category, an instruction provider, or a message class. The
   *  wording is the client's. */
  id: string;
  group: "brief" | "tools" | "messages";
  tokens: number;
  /** The population behind the figure (tools in a category); null where it is not
   *  measured. */
  count: number | null;
}

/** How full a model's context window is after a turn. The backend owns the derivation
 *  (used tokens, fraction, severity); clients only render it. Null on a metrics frame
 *  when unmeasurable (no window or no token usage). */
export interface ContextWindow {
  /** Tokens occupying the window (prompt + generation). */
  used: number;
  /** The model's context window. */
  window: number;
  /** How full the window is, 0–1. */
  fraction: number;
  /** Window severity per the backend's thresholds. */
  level: "nominal" | "warn" | "alert";
  /** What `used` is made of. Null when unmeasurable (all turns predate the measurement,
   *  or no turn has run since the process started). Never zeroed: a split claiming no
   *  tools and no brief would be false. */
  parts: ContextComposition | null;
  /** Where compaction will fold this thread, and whether it is armed. Per-thread: the
   *  operator's threshold with the conversation's own override applied, so a client must
   *  not substitute the settings page's value. Null means no policy was reported, not
   *  that folding is off (that is `active: false`, so the mark dims instead of vanishing).
   *  Separate from `level`: an alert is a notification, a fold acts on the thread. */
  fold?: FoldPoint | null;
}

/** See `ContextWindow.fold`. Mirrors `FoldPoint` in `backend/runs/events/`. */
export interface FoldPoint {
  /** The share of the window at which the fold fires, 0–1. */
  fraction: number;
  /** Whether folding is switched on for this thread. */
  active: boolean;
}

/** What the thread's most recent model request cost, on its own; everything else on
 *  `RunMetrics` is cumulative. Answers which endpoint served it (a fallback chain's
 *  second model vanishes into a sum) and how much of its prompt was cached. Null fields
 *  mean the provider reported nothing, never zero. */
export interface LastRequestUsage {
  /** The model that actually answered, prefixed by its provider when one is reported
   *  (`openai:qwen3-32b`). Not necessarily the model the thread is bound to. */
  route: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
}

/** What the thread has cost so far, cumulative over the conversation rather than the
 *  run. Every derived figure is computed server-side. Null means unmeasured and is never
 *  interchangeable with 0 (see `RunMetrics` in `backend/runs/events/`). */
export interface RunMetrics extends Base {
  type: "run.metrics";
  steps: number;
  tool_calls: number;
  /** Completed operator exchanges, where `steps` counts the model round-trips. */
  turns: number;
  input_tokens: number | null;
  output_tokens: number | null;
  /** Provider-reported cached prompt tokens; null when the endpoint reports none. */
  cache_read_tokens: number | null;
  /** Wall-clock measured by the backend around its own streaming. `llm_ms` is the full
   *  round-trip, connect and queue included. */
  llm_ms: number | null;
  tool_ms: number | null;
  ttft_ms_total: number | null;
  ttft_samples: number;
  /** Cached share of prompt tokens (0–1), mean time to first content, and generation
   *  throughput against model time. */
  cache_hit_ratio: number | null;
  ttft_avg_ms: number | null;
  output_tokens_per_second: number | null;
  /** Mirrors the backend; the gauge renders the derived `context` field instead. */
  context_window: number | null;
  /** The footprint after this turn (last response's prompt + generation). Mirrors the
   *  backend; the gauge renders `context`. */
  context_used: number | null;
  /** Context-window fullness after this turn, or null when unmeasurable. */
  context: ContextWindow | null;
  /** Null on a thread that has never produced a response. */
  last_request?: LastRequestUsage | null;
}
/** Which bound stopped a blocked run. */
export type RunEndCode =
  | "wall_clock"
  | "inactivity"
  | "first_token"
  | "loop"
  | "context_overflow"
  | "turn_limit";
export interface RunEnded extends Base {
  type: "run.ended";
  outcome: "done" | "blocked" | "cancelled";
  detail: string | null;
  /** Absent from an older backend and from endings that no bound caused. */
  code?: RunEndCode | null;
}
export interface RunError extends Base {
  type: "run.error";
  message: string;
  kind: string | null;
}

// --- Step boundaries -------------------------------------------------------
export interface StepStarted extends Base {
  type: "step.started";
  index: number;
  title: string | null;
}
export interface StepCompleted extends Base {
  type: "step.completed";
  index: number;
}

// --- Content (reasoning / answer split) ------------------------------------
export interface ThinkingDelta extends Base {
  type: "thinking.delta";
  text: string;
}
export interface AnswerDelta extends Base {
  type: "answer.delta";
  text: string;
}
/** The trailing text and reasoning streamed for a model request that failed and is being
 *  sent again; the retry streams its own. */
export interface AnswerRetracted extends Base {
  type: "answer.retracted";
  text_chars: number;
  thinking_chars: number;
}

// --- Tools -----------------------------------------------------------------
/** On a call made from inside a `run_code` script: that call's `tool_call_id`. Absent or
 *  null for a call the model made directly. */
interface Nestable {
  parent_tool_call_id?: string | null;
}
export interface ToolStarted extends Base, Nestable {
  type: "tool.started";
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
}
/** The model has named a tool and is still writing its arguments. `args_chars` rises in
 *  throttled steps; `tool.started` with the same id supersedes it. */
export interface ToolPreparing extends Base {
  type: "tool.preparing";
  tool_call_id: string;
  name: string;
  args_chars: number;
  /** The id the call was announced under before the stream changed it. */
  replaces?: string | null;
}
export interface ToolProgress extends Base {
  type: "tool.progress";
  tool_call_id: string;
  elapsed_s: number | null;
  partial: string | null;
}
/** One typed slice of a wrapped payload. `untrusted` was fenced for the model and
 *  `source` is the fence's attribute; `context_file` is a project instruction file at
 *  `path`. The cold DTOs carry the same shape. */
export interface ContentPartWire {
  kind: "text" | "untrusted" | "context_file";
  text: string;
  source: string | null;
  path: string | null;
}
export interface ToolCompleted extends Base, Nestable {
  type: "tool.completed";
  tool_call_id: string;
  name: string;
  result: unknown;
  /** The result split into typed parts; `result` stays the raw payload. */
  result_parts?: ContentPartWire[] | null;
  /** Images the call handed back for the model to look at — a screenshot. Base64,
   *  media type alongside; the renderer adds the data-URI scheme. */
  images?: ToolImage[];
}
export interface ToolImage {
  media_type: string;
  data: string;
}
export interface ToolFailed extends Base, Nestable {
  type: "tool.failed";
  tool_call_id: string;
  name: string;
  error: string;
  /** The failure is the platform's own fault. Absent from older backends. */
  platform?: boolean;
}

// --- Browser ---------------------------------------------------------------
/** A tool attached a browser to the conversation; not sent when a call reuses one. */
export interface BrowserOpened extends Base {
  type: "browser.opened";
}

// --- Plugin windows --------------------------------------------------------
/** The agent asked for a plugin's window to open over the app. */
export interface PluginWindowOpened extends Base {
  type: "plugin.window_open";
  conversation_id: string;
  plugin_id: string;
  plugin: string;
  window: string;
  title: string;
  width: number;
  height: number;
  min_width: number;
  min_height: number;
  resizable: boolean;
}

// --- View (the conversation's one versioned output surface) ----------------
// The View's live head — a running server reverse-proxied at `url`. `url` already
// carries the entry path when one was given, so it renders the page, not a listing.
export interface ViewLive extends Base {
  type: "view.live";
  conversation_id: string;
  url: string;
  title: string | null;
  command: string;
  port: number;
}
export interface ViewLiveStopped extends Base {
  type: "view.live.stopped";
  conversation_id: string;
}
// A new View version, minted by a `show`; its code is browsed via
// `/views/snapshots/{id}/…`. `preview_artifact_id` + `preview_kind` point at the captured
// bytes of a `show(file=…)` (`/views/{preview_artifact_id}/content`), or are both null
// for a live/auto preview.
export interface ViewSnapshot extends Base {
  type: "view.snapshot";
  conversation_id: string;
  snapshot_id: string;
  title: string | null;
  created_at: string;
  files_changed: number;
  summary: string;
  preview_kind: "html" | "image" | "text" | "other" | null;
  preview_artifact_id: string | null;
}

// --- Conversation ----------------------------------------------------------
export interface ConversationTitled extends Base {
  type: "conversation.titled";
  conversation_id: string;
  title: string;
}

/** Why a fold happened. `threshold`: the projected footprint reached the operator's
 *  percentage before the turn. `overflow`: the provider refused the request as too large,
 *  so the chassis folded and retried in the same turn. `manual`: COMPACT NOW. Mirrors
 *  backend/runs/events/. */
export type CompactionReason = "threshold" | "overflow" | "manual";

/** One section of a compaction summary, parsed by the backend, not here: the section
 *  roster is a security boundary (a heading copied out of a fetched page must not open a
 *  section and carry what follows out of the untrusted fence), so the one parser lives in
 *  `backend/core/compaction_sections.py` and is not mirrored. Mirrors `SummarySection`
 *  there.
 *
 *  `key` is the heading verbatim, or empty for the single section a checkpoint that
 *  parses into nothing degrades to; render that one without a heading. `body` never
 *  carries a fence marker or the model-facing preamble. */
export interface SummarySection {
  key: string;
  body: string;
  /** Repeats what a tool, file or web page returned, so it is shown attributed. Set by
   *  section identity, not by finding a fence: checkpoints written before fencing still
   *  quote pages. */
  untrusted: boolean;
  /** `"machine"` is Anchors (exact paths, ids and numbers); everything else is prose. */
  voice: "prose" | "machine";
}

/** A fold is starting. Emitted before the summarizer call, which can take tens of seconds,
 *  so the turn does not stall silently. `messages` and `tokens_estimate` are what is about
 *  to be folded; the settled figures ride on `conversation.compacted`. */
export interface CompactionStarted extends Base {
  type: "compaction.started";
  conversation_id: string;
  reason: CompactionReason;
  messages: number;
  tokens_estimate: number;
}

/** A piece of the summary as the summarizer writes it, between `compaction.started` and
 *  `conversation.compacted`. This is the model's working, not the checkpoint: the text is
 *  raw (a leaked `<think>` block is not stripped, anchors are not merged, the tool-facts
 *  section is not fenced). Render it live and replace it wholesale when
 *  `conversation.compacted` lands; never store one. */
export interface CompactionDelta extends Base {
  type: "compaction.delta";
  conversation_id: string;
  text: string;
}

/** A fold that started and did not land (the summarizer failed or wrote nothing usable);
 *  the thread is unchanged. It closes the pair `compaction.started` opened, so the live
 *  fold turn that event seated must be removed. `reason` repeats the opening frame's. */
export interface CompactionFailed extends Base {
  type: "compaction.failed";
  reason: CompactionReason;
  message: string;
}

/** Earlier turns were folded into a summary before this turn ran. The transcript keeps
 *  every turn; this marks where the model's replayed view narrows to `summary` plus the
 *  turns after it. Emitted mid-run before the answer streams, and persisted as its own
 *  message so a reload renders the same divider. */
export interface ConversationCompacted extends Base {
  type: "conversation.compacted";
  conversation_id: string;
  /** The checkpoint message the summary is stored on; the divider's key, and what a cold
   *  read returns. */
  message_id: string;
  summary: string;
  /** How many messages the summary stands in for: `ModelMessage`s, not exchanges (a plain
   *  exchange is two), so never render it as a turn count. */
  messages_compacted: number;
  /** Coarse char-based estimates of what the fold replaced (over the folded messages) and
   *  what replaced it (over the stored summary alone, not the next turn's footprint).
   *  Always sent, 0 when nothing to report. */
  tokens_before?: number | null;
  tokens_after?: number | null;
  /** The rendered turn the divider follows. The backend resolves the position because a
   *  tree node is not a rendered message. Null appends. */
  after_message_id: string | null;
  /** What triggered the fold; pairs with the opening `compaction.started`. Optional for
   *  older backends, which only meant the ordinary case. */
  reason?: CompactionReason;
  /** `summary` split into divider sections, by the same function the cold read uses so
   *  live and reload agree. The divider falls back to `summary` when absent. */
  sections?: SummarySection[];
  /** What the fold re-read after the briefing: home paths, and `plan` when the plan came
   *  back. Optional for older backends. */
  restored?: string[];
}

// --- Context ---------------------------------------------------------------
/** Something the chassis put in front of the model that nobody in the conversation wrote
 *  (a project's instruction files, the skill catalog, the plan reminder, the date). The
 *  gauge says what it costs; this says what it said. Emitted as the turn is assembled, and
 *  must never render as a tool call: the chassis is reaching in, not the model out.
 *
 *  `contributor` is the `ContextSegment.id` slug for the standing brief's rows.
 *  `placement`: `instructions` at the head (re-sent every turn; churn invalidates the
 *  prompt-prefix cache), `prompt` at the tail of the turn's message (volatile content
 *  kept out of that prefix), or `summary` on a compaction checkpoint after the briefing.
 *  `tokens` is a coarse estimate over the whole block; `text` is capped for the wire,
 *  flagged by `truncated`. */
export interface ContextInjected extends Base {
  type: "context.injected";
  contributor: string;
  placement: "instructions" | "prompt" | "summary";
  tokens: number;
  text: string;
  truncated: boolean;
}

// --- Notices ---------------------------------------------------------------
/** A source the turn met, and how far it got with it.
 *
 *  `key` identifies the source (a URL for a page, `source:ref` for a corpus passage) and
 *  is what repeat sightings fold by; web and corpus citations fold by different fields.
 *
 *  `engagement` is the rung this sighting reached: `listed` < `read` (its text reached the
 *  model) < `cited` (something asserted rests on it). One source may arrive on several
 *  frames; the consumer keeps the highest.
 *
 *  `url` is null for a corpus passage (locator is `source_id` + `ref`), so read `kind`
 *  before rendering a link. `published` is the provider's date string, unparsed.
 *  `snippet` is already unfenced and capped. */
export interface CitationAdded extends Base {
  type: "citation.added";
  url: string | null;
  title: string | null;
  key: string;
  kind: "web" | "corpus";
  engagement: "listed" | "read" | "cited";
  snippet: string | null;
  published: string | null;
  retrieved_at: string | null;
  source_id: string | null;
  ref: string | null;
}
export interface ApprovalRequired extends Base {
  type: "approval.required";
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  summary: string;
  explanation: string | null;
  /** Why this call always asks. Set, it is once-only: no standing approval is offered.
   *  Absent from an older backend. */
  guard?: string | null;
  /** Set on a host command about to run with no fence in force, saying why. */
  unconfined?: string | null;
}
/** One answer offered for a question. */
export interface QuestionOption {
  label: string;
  description: string | null;
}
/** `multi_select` says whether several `options` may be chosen together; writing an
 *  answer instead is always allowed. */
export interface QuestionSpec {
  question: string;
  options: QuestionOption[];
  multi_select: boolean;
}
/** The turn is parked on the operator answering, not permitting. Separate from
 *  `approval.required` because the reply is a value that becomes the tool's result, not a
 *  yes or no. One event per call, holding every question. */
export interface QuestionAsked extends Base {
  type: "question.asked";
  tool_call_id: string;
  questions: QuestionSpec[];
}
/** The operator's answer to one question of a parked `ask_user` call. The question rides
 *  along because the backend pairs it from the parked call's arguments, never from what
 *  the client posted. */
export interface AnsweredQuestion {
  question: string;
  selections: string[];
  text?: string | null;
}
/** The park was answered. The model already gets the answer as the deferred call's
 *  result; this exists so the transcript can show it without the client parsing backend
 *  prose (the same pairing feeds a cold load, so live and reloaded renders agree). */
export interface QuestionAnswered extends Base {
  type: "question.answered";
  tool_call_id: string;
  answers: AnsweredQuestion[];
}
/** An action at the Auto permission level is being ruled on in the operator's place.
 *  Announced before the ruling so a review that takes a model call reads as work in
 *  progress. `summary` is the action's worst case, the same words the reviewer judges.
 *
 *  `detail` is the act's own content where the tool has some worth reading (a sub-agent's
 *  task, a skill's rewritten text, the reason for opening a credential); null for most
 *  tools. It is what the reviewer ruled on beyond the summary.
 *
 *  `reach` is how far a shell command declared it needs to go: the worktree, the network,
 *  or the operator's machine. Null for acts that declare nothing (a mail send, a file
 *  write), which is not the same as declaring the widest reach. */
export interface ReviewStarted extends Base {
  type: "review.started";
  tool_call_id: string;
  name: string;
  summary: string;
  detail: string | null;
  reach: "offline" | "network" | "host" | null;
  /** Whether a VM command will run with egress, which an `offline` declaration can
   *  differ from. Null where nothing runs in the VM. */
  network?: boolean | null;
}
/** How the review ruled, and on what.
 *
 *  `decision`: `allow` ran the call with no prompt, `ask` parked it for the operator.
 *  `block` is no longer produced (an unrecoverable act goes to the operator like any
 *  other it will not clear) but stays because stored events of older threads carry it.
 *  `stage` says whether the structural judge or a model settled it, `tier` on which
 *  ground when the judge did, `fenced` whether an OS fence held the command to what it
 *  declared, and the three axes what the model saw.
 *
 *  The axes are null when the model stage never ran (the judge cleared it, or there was
 *  nothing to review with); `tier` is null whenever the model settled it. */
export interface ReviewCompleted extends Base {
  type: "review.completed";
  tool_call_id: string;
  name: string;
  decision: "allow" | "ask" | "block";
  stage: "judge" | "reviewer";
  reason: string;
  tier: "read" | "sandbox" | "workspace" | null;
  fenced: boolean;
  risk: "low" | "high" | "too_destructive" | null;
  authorization: "explicitly_no" | "neutral" | "explicitly_yes" | null;
  correctness: string | null;
  /** Optional: a replayed older event carries neither field. */
  scores?: Record<string, number> | null;
  setup?: "unbound" | "unreachable" | null;
}
/** Who a queued message is from. `operator` is someone typing during a run; `subagent` is
 *  a report from an agent the thread launched; `agent` is one agent in the tree messaging
 *  another, named by `sender` on the queue frame. All are handed to the next, not-yet-sent
 *  request, so none interrupts a model mid-stream, but they must not read the same: a
 *  report shown as the operator's words misstates who said what.
 *
 *  `subagent` and `agent` render as an incoming `subagent` row. `system` is a chassis
 *  reminder (see `notice` on the queue frame) and renders as a `notice` row. */
export type MessageSource = "operator" | "subagent" | "agent" | "system";
/** A message arrived mid-run and is queued for injection at the next model-request
 *  boundary. `text` rides inline so a reattaching client rebuilds the bubble from
 *  replay. */
export interface MessageQueued extends Base {
  type: "message.queued";
  message_id: string;
  text: string;
  source?: MessageSource;
  /** The handle of the agent that sent it, when `source` is `agent`. */
  sender?: string | null;
  /** An operator message carrying attachments, file refs or a command: it opens a turn
   *  of its own after this one instead of joining it. */
  turn?: boolean;
  /** The notice's kind (`open-tasks`), when `source` is `system`. */
  notice?: string | null;
}
/** The operator rewrote a queued message's text before the run consumed it.
 *  `text` is the full replacement (not a delta), inline so a reattaching client
 *  rebuilds the pending bubble purely from replay. */
export interface MessageEdited extends Base {
  type: "message.edited";
  message_id: string;
  text: string;
}
/** The operator withdrew a queued message before the run consumed it. */
export interface MessageWithdrawn extends Base {
  type: "message.withdrawn";
  message_id: string;
}
/** The run was asked to hold a queued message back, or to stop holding it. Separate from
 *  `message.edited` because a hold is a state, not new text. The hold must be the run's:
 *  the drain is server-side, so a client merely declining to show the message as sendable
 *  would still watch it go. */
export interface MessageHeld extends Base {
  type: "message.held";
  message_id: string;
  held: boolean;
}
/** A queued message was handed to the model, in drain order; it then persists as a
 *  normal user message. */
export interface MessageInjected extends Base {
  type: "message.injected";
  message_id: string;
  /** Repeated from the queue frame so a client attaching after that frame left its replay
   *  window still knows whose message landed. */
  source?: MessageSource;
}
/** The operator stopped the turn, so the inbox holds everything until they send again
 *  (`waiting`), or they did and it flows again. */
export interface InboxWaiting extends Base {
  type: "inbox.waiting";
  waiting: boolean;
}
/** One task on the agent's running list. `blocked` only occurs when the backend enables
 *  subtasks/dependencies. */
export interface TaskItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled" | "blocked";
  /** Present-tense label for the task while it runs ("Reading the config"). */
  active_form?: string | null;
}
/** The whole list, not a delta, so applying it is idempotent on replay. */
export interface TasksUpdated extends Base {
  type: "tasks.updated";
  items: TaskItem[];
}
/** The written plan a plan-mode turn produced, or its status changing. Unlike the task
 *  checklist, this is the document the operator approves. `revision` counts up on each
 *  resubmission so the panel treats a revised plan as a new arrival. */
export interface PlanUpdated extends Base {
  type: "plan.updated";
  title: string;
  body: string;
  steps: string[];
  status: PlanStatus;
  revision: number;
}
export type PlanStatus = "pending" | "approved" | "revising" | "denied";
/** The thread's permission level changed from inside the run (`plan_enter` narrowing it,
 *  or an approved plan raising it). Load-bearing: the client sends its level back on every
 *  message and would otherwise overwrite the one the agent just set. */
export interface PermissionChanged extends Base {
  type: "permission.changed";
  level: string;
  /** Why, in one line, for the transcript — "entering plan mode", "plan approved". */
  reason: string;
}
export interface LimitNotice extends Base {
  type: "limit.notice";
  /** "context": the context window was exceeded and the run stops. "search" is no longer
   *  emitted but stays handled because the wire never narrows. Mirrors
   *  backend/runs/events/. */
  limit:
    | "steps"
    | "tool_calls"
    | "tokens"
    | "time"
    | "loop"
    | "verify"
    | "context"
    | "search"
    /** The agent's own request cap was reached; the work so far is partial. */
    | "turn_limit"
    /** Not a stop: the turn carried on past a bound by itself (auto-continue). */
    | "continue";
  message: string;
  /** The stop marker, when the bound also blocked the turn; the string persisted as
   *  `blocked_reason`. Two context stops share one `limit` but not one remedy, so the
   *  toast keys the remedy off this. */
  detail?: string | null;
}

/** The `run.ended` detail (and persisted `blocked_reason`) when the provider refused the
 *  request as too large and the chassis could not fold its way out. Mirrored verbatim from
 *  `agent/model_errors.py`: the blocked turn's "Compact and retry" control keys on this
 *  string, live and on reload. Any other stop offers Continue alone. */
export const CONTEXT_OVERFLOW_DETAIL = "context window exceeded";

/** The same ceiling reached after the turn already folded and retried. Mirrored from
 *  `agent/model_errors.py`; "Compact and retry" must not be offered here since it would
 *  re-send the same oversized request. Reads as a plain stop, and the context toast drops
 *  its remedy sentence. */
export const CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL =
  "context window exceeded after compaction";

export type RunEvent =
  | RunQueued
  | RunStarted
  | RunMetrics
  | RunEnded
  | RunError
  | StepStarted
  | StepCompleted
  | ThinkingDelta
  | AnswerDelta
  | AnswerRetracted
  | ToolPreparing
  | ToolStarted
  | ToolProgress
  | ToolCompleted
  | ToolFailed
  | BrowserOpened
  | PluginWindowOpened
  | ViewLive
  | ViewLiveStopped
  | ViewSnapshot
  | ConversationTitled
  | CompactionStarted
  | CompactionDelta
  | CompactionFailed
  | ConversationCompacted
  | ContextInjected
  | CitationAdded
  | ApprovalRequired
  | QuestionAsked
  | QuestionAnswered
  | ReviewStarted
  | ReviewCompleted
  | MessageQueued
  | MessageEdited
  | MessageHeld
  | MessageWithdrawn
  | MessageInjected
  | InboxWaiting
  | TasksUpdated
  | PlanUpdated
  | PermissionChanged
  | LimitNotice;

/** A run is over after one of these — the stream reader stops. */
export function isTerminal(event: RunEvent): boolean {
  return event.type === "run.ended" || event.type === "run.error";
}
