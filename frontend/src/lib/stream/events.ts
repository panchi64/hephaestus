/**
 * The run event protocol — the client mirror of `backend/runs/events/` (v1).
 *
 * Every frame on a run's SSE stream is one of these, a flat envelope
 * `{ type, seq, ts, ...payload }`. This is the frozen contract the chat/run
 * controllers fold into their stores; keep it in lockstep with the backend union.
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
/** How full a model's context window is after a turn. The backend owns the
 *  whole derivation (used tokens, fraction, severity); clients only render it.
 *  Null on a metrics frame when unmeasurable (no window or no token usage). */
/** What the occupied part of the window is holding, three ways.
 *
 *  The three are exhaustive — the backend scales them to sum to `ContextWindow.used` —
 *  so they can be read as a whole with no unexplained remainder.
 *
 *  Every figure is an **estimate anchored to the provider's total**: no provider reports
 *  a breakdown, so the split is measured backend-side from what it assembled and scaled
 *  to the one number the provider does report. Render them with a `~`. */
export interface ContextComposition {
  /** The standing brief: instructions + system prompt. */
  system: number;
  /** Every tool name, description and JSON schema handed to the model. */
  tools: number;
  /** The conversation itself. */
  messages: number;
  /** The same tokens itemised — one row per tool category, per contributor to the
   *  standing brief, per class of message content. Each belongs to exactly one group,
   *  and a group's segments sum to its total above, so the two resolutions can never
   *  disagree. Empty when the backend could measure the totals but not the detail. */
  segments: ContextSegment[];
}

/** One line item inside a group.
 *
 *  **A segment is here because it weighs something.** The backend emits no zero rows and
 *  no fixed roster: a thread that has called no tools carries no `tool_results` segment,
 *  a catalog with no MCP servers connected carries no `external` one, and each appears
 *  the moment it starts costing the window. So the panel renders whatever arrives — it
 *  never expects a particular id, and it never draws a row for one that didn't come. */
export interface ContextSegment {
  /** A backend slug — a tool category as the operator's own tool settings name it, an
   *  instruction provider, or a message class. The wording is the client's. */
  id: string;
  group: "brief" | "tools" | "messages";
  tokens: number;
  /** The population behind the figure where there is one (tools in a category); null
   *  where counting would be a claim we haven't measured. */
  count: number | null;
}

export interface ContextWindow {
  /** Tokens occupying the window (prompt + generation). */
  used: number;
  /** The model's context window. */
  window: number;
  /** How full the window is, 0–1. */
  fraction: number;
  /** Window severity per the backend's thresholds. */
  level: "nominal" | "warn" | "alert";
  /** What `used` is made of. Null when it couldn't be measured — a thread whose turns
   *  all predate the measurement, or a reload in a process where no turn has run yet.
   *  Absent, never zeroed: a split claiming no tools and no brief would be a confident
   *  lie about the one thing this exists to expose. */
  parts: ContextComposition | null;
  /** Where conversation compaction will fold *this thread*, and whether it is armed.
   *
   *  Per-thread, not the global setting: it is the operator's threshold with the
   *  conversation's own on/off override already folded in, so a client must not
   *  substitute what the settings page says. Null when the backend had no policy to
   *  report — which is **not** the same as folding being off; that arrives as
   *  `active: false` with the fraction still present, so the mark can be dimmed rather
   *  than dropped. Optional so an older backend still renders the gauge.
   *
   *  Deliberately absent from `level`: an alert is a notification and a fold is an act
   *  on the thread, so the two are separate dials and the gauge shows both. */
  fold?: FoldPoint | null;
}

/** See `ContextWindow.fold`. Mirrors `FoldPoint` in `backend/runs/events/`. */
export interface FoldPoint {
  /** The share of the window at which the fold fires, 0–1. */
  fraction: number;
  /** Whether folding is switched on for this thread. */
  active: boolean;
}

/** What the thread has cost so far — **cumulative over the conversation**, not the
 *  run. The backend counts the active path and measures its own wall-clock; every
 *  derived figure below (the ratio, the average, the rate) is computed server-side.
 *  Null means unmeasured and is never interchangeable with 0 — see `RunMetrics` in
 *  `backend/runs/events/`. */
/** What the thread's most recent model request cost, on its own — everything else on
 *  `RunMetrics` is cumulative over the conversation.
 *
 *  The two questions a running total cannot answer: which endpoint served that last
 *  request (a fallback chain's second model disappears into a sum) and how much of its
 *  prompt the provider had cached (a cold turn disappears into a warm average). Null
 *  fields mean the provider reported nothing, never zero. */
export interface LastRequestUsage {
  /** The model that actually answered, prefixed by its provider when one is reported
   *  (`openai:qwen3-32b`). Not necessarily the model the thread is bound to. */
  route: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
}

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
  /** Wall-clock measured by the backend around its own streaming, so it means the
   *  same on every provider. `llm_ms` is the full round-trip, connect and queue
   *  included — the wait the operator actually sat through. */
  llm_ms: number | null;
  tool_ms: number | null;
  ttft_ms_total: number | null;
  ttft_samples: number;
  /** Backend-derived: cached share of prompt tokens (0–1), mean time to first
   *  content, and generation throughput against model time. */
  cache_hit_ratio: number | null;
  ttft_avg_ms: number | null;
  output_tokens_per_second: number | null;
  /** The model's context window, when known — the ceiling `context` measures
   *  against. Mirror completeness; the gauge renders the derived `context` field. */
  context_window: number | null;
  /** The context footprint after this turn (last response's prompt + generation).
   *  Mirror completeness; the gauge renders the derived `context` field. */
  context_used: number | null;
  /** The context-window fullness after this turn, or null when unmeasurable. */
  context: ContextWindow | null;
  /** The last model request on its own. Null on a thread that has never produced a
   *  response; optional here only so an older backend still renders. */
  last_request?: LastRequestUsage | null;
}
export interface RunEnded extends Base {
  type: "run.ended";
  outcome: "done" | "blocked" | "cancelled";
  detail: string | null;
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

// --- Tools -----------------------------------------------------------------
/** The field every lifecycle frame of a tool call carries when the call was made from
 *  inside a script — a `run_code` call's own tool calls. It names that `run_code`
 *  call's `tool_call_id`; absent or null for a call the model made directly. */
interface Nestable {
  parent_tool_call_id?: string | null;
}
export interface ToolStarted extends Base, Nestable {
  type: "tool.started";
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
}
export interface ToolProgress extends Base {
  type: "tool.progress";
  tool_call_id: string;
  elapsed_s: number | null;
  partial: string | null;
}
export interface ToolCompleted extends Base, Nestable {
  type: "tool.completed";
  tool_call_id: string;
  name: string;
  result: unknown;
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
// A new **version** of the View — minted by a `show`. Captures the agent's sandbox
// tree (the version's code, browsed/diffed via `/views/snapshots/{id}/…`) and how it
// previews: `preview_artifact_id` + `preview_kind` point at the captured-bytes
// preview of a `show(file=…)` (bytes at `/views/{preview_artifact_id}/content`), or
// both null for a live/auto preview. Conversation-scoped (not a message block).
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

/** Why a fold happened. `threshold` is the ordinary one — the projected footprint
 *  reached the operator's percentage before the turn ran. `overflow` is the recovery
 *  path: the provider refused the request as too large, so the chassis folded and
 *  retried inside the same turn. `manual` is COMPACT NOW. The operator reads a very
 *  different story into each, so the wire carries which rather than leaving the UI to
 *  infer it from timing. Mirrors backend/runs/events/. */
export type CompactionReason = "threshold" | "overflow" | "manual";

/** One section of a compaction summary, parsed by the backend rather than here.
 *
 *  The eight headings are a contract the summarizer is held to, and the roster is a
 *  **security boundary**: only a heading naming one of *our* sections ends a section, so a
 *  heading the summarizer copied out of a fetched page cannot open one of its own and
 *  carry what follows it out of the untrusted fence. That rule lives in
 *  `backend/core/compaction_sections.py` and is deliberately not mirrored here — one
 *  parser, in the language that also writes the fence.
 *
 *  `key` is the heading verbatim, or **empty** for the single section a checkpoint that
 *  parses into nothing degrades to (an old checkpoint, or a summarizer that drifted off
 *  format) — render that one without a heading. `body` never carries a fence marker or the
 *  model-facing preamble. Mirrors `SummarySection` in `backend/core/compaction_sections.py`. */
export interface SummarySection {
  key: string;
  body: string;
  /** Repeats what a tool, file or web page returned, so it is shown attributed rather
   *  than in the workspace's own voice. Set by *section identity*, not by finding a
   *  fence — a checkpoint written before fencing existed still quotes a page. */
  untrusted: boolean;
  /** How the section wants to be set. `"machine"` is Anchors, whose whole purpose is
   *  exact paths, ids and numbers; everything else is prose. */
  voice: "prose" | "machine";
}

/** A fold is starting. Emitted *before* the summarizer call, which can take tens of
 *  seconds on a long thread — without it the turn simply stalls with nothing on
 *  screen, and the only account of the pause arrives after it is over. `messages` and
 *  `tokens_estimate` are what is about to be folded, so the row can say what the wait
 *  is for; the settled figures ride on `conversation.compacted`. */
export interface CompactionStarted extends Base {
  type: "compaction.started";
  conversation_id: string;
  reason: CompactionReason;
  messages: number;
  tokens_estimate: number;
}

/** A piece of the summary, as the summarizer writes it.
 *
 *  `compaction.started` says a fold began and `conversation.compacted` says it finished;
 *  this is the part in between, which on a local endpoint is tens of seconds long and
 *  until now carried nothing at all.
 *
 *  **This is the model's working, not the checkpoint.** The text arrives raw — a leaked
 *  `<think>` block is not stripped from it, carried-forward anchors are not merged in, and
 *  the tool-facts section is not yet fenced, because none of those can be done to a
 *  fragment. So render it live and replace it wholesale when `conversation.compacted`
 *  lands; never store one, and never treat one as the summary the model will read. */
export interface CompactionDelta extends Base {
  type: "compaction.delta";
  conversation_id: string;
  text: string;
}

/** A fold that started and then did not land — the summarizer failed or wrote nothing
 *  usable. The thread is exactly as it was: nothing was folded and nothing was deleted.
 *  It closes the pair `compaction.started` opened, which is why the live fold turn that
 *  event seated has to come back out; `reason` is the same trigger the opening frame
 *  carried, and `message` is the backend's own account of what went wrong. */
export interface CompactionFailed extends Base {
  type: "compaction.failed";
  reason: CompactionReason;
  message: string;
}

/** The thread's earlier turns were folded into a summary before this turn ran,
 *  because its context footprint reached the operator's threshold. Nothing was
 *  deleted — the transcript keeps every turn; this marks where the *model's*
 *  replayed view narrows to `summary` plus the turns after it. Emitted mid-run,
 *  before the answer streams, and persisted as its own message so a reload
 *  renders the same divider. */
export interface ConversationCompacted extends Base {
  type: "conversation.compacted";
  conversation_id: string;
  /** The checkpoint message the summary is stored on — the id the divider is
   *  keyed by, and the same one a cold read returns. */
  message_id: string;
  summary: string;
  /** How many **messages** the summary stands in for, so the divider can say so
   *  without the client counting anything itself. `ModelMessage`s, not exchanges —
   *  a plain exchange is two — so never render this as a turn count. */
  messages_compacted: number;
  /** Coarse char-based estimates of what the fold replaced (`tokens_before`, over
   *  the folded messages) and what replaced it (`tokens_after`, over the stored
   *  summary alone — *not* the next turn's footprint, which also carries whatever
   *  tail the backend retained). Always sent, 0 when there's nothing to report;
   *  optional here only so an older backend still renders. */
  tokens_before?: number | null;
  tokens_after?: number | null;
  /** The rendered turn the divider follows. The backend resolves the position —
   *  a tree node is not a rendered message, so the client would otherwise have to
   *  guess and could land somewhere a reload disagrees with. Null ⇒ append. */
  after_message_id: string | null;
  /** What triggered the fold, paired with the `compaction.started` that opened it.
   *  Optional only so an older backend still renders the divider — it defaults to the
   *  ordinary case there, which is what such a backend could only have meant. */
  reason?: CompactionReason;
  /** `summary` split into the sections the divider renders. Derived on the backend from
   *  the very string on this frame, and by the same function the cold read uses, so the
   *  divider a live client draws and the one a reload draws cannot disagree. Optional so
   *  an older backend still renders — the divider falls back to `summary` then. */
  sections?: SummarySection[];
}

// --- Context ---------------------------------------------------------------
/** Something the chassis put in front of the model that nobody in the conversation
 *  wrote — a project's instruction files, the skill catalog, the plan reminder, the date.
 *
 *  The context gauge says what these *cost*; this says what they *said*, and when they
 *  arrived. It is emitted as the turn is assembled, so it lands in the work log ahead of
 *  the work it shaped — and it must never render as a tool call: a tool call is the model
 *  reaching out, this is the chassis reaching in.
 *
 *  `contributor` is the same slug `ContextSegment.id` carries for the standing brief's
 *  rows, so the gauge's "Skill catalog · ~4k" and this row are visibly one block seen
 *  from two distances. `placement` is where it landed in the request — `instructions` at
 *  the head (re-sent every turn, invalidating the prompt-prefix cache when it churns) or
 *  `prompt` at the tail of the turn's own message (volatile content kept out of that
 *  cacheable prefix). `tokens` is a coarse estimate over the **whole** block; `text` is
 *  capped for the wire and says so via `truncated`. */
export interface ContextInjected extends Base {
  type: "context.injected";
  contributor: string;
  placement: "instructions" | "prompt";
  tokens: number;
  text: string;
  truncated: boolean;
}

// --- Notices ---------------------------------------------------------------
/** A source the turn met, and how far it got with it.
 *
 *  `key` is what the source *is* — a URL for a page, `source:ref` for a corpus passage —
 *  and is what repeat sightings fold by. It is on the wire because "these two rows are
 *  one source" is a claim about the sources, and a web citation and a corpus one do not
 *  fold by the same field.
 *
 *  `engagement` is the rung this sighting reached: `listed` (a search returned it, nobody
 *  opened it) < `read` (its text was put in front of the model) < `cited` (something
 *  asserted rests on it). One source may arrive on several frames — listed by a search,
 *  then read by a fetch — and the consumer keeps the highest.
 *
 *  `url` is null for a corpus passage, which has a locator (`source_id` + `ref`) rather
 *  than an address, so read `kind` before rendering a link. `published` is the source's
 *  own date exactly as its provider reported it — a bare string, never parsed by the
 *  backend. `snippet` is already unfenced and capped. */
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
}
/** One answer offered for a question. */
export interface QuestionOption {
  label: string;
  description: string | null;
}
/** One question the operator is being asked. `multi_select` says whether several of
 *  `options` may be chosen together; writing an answer instead is always allowed and so
 *  is never a flag. */
export interface QuestionSpec {
  question: string;
  options: QuestionOption[];
  multi_select: boolean;
}
/** The turn is parked on the operator answering, not on them permitting.
 *
 *  Its own event rather than an `approval.required` with a different shape: an approval
 *  is a yes or a no about an action already decided on, and a question comes back
 *  carrying a *value* that becomes the tool's result. One event per call, holding every
 *  question in it — the model asks in one go and is answered in one go. */
export interface QuestionAsked extends Base {
  type: "question.asked";
  tool_call_id: string;
  questions: QuestionSpec[];
}
/** What the operator said to one question of a parked `ask_user` call. The question's own
 *  words ride with the answer because the backend pairs them from the *parked call's
 *  arguments* — never from what the client posted — so the transcript restates what was
 *  actually asked. */
export interface AnsweredQuestion {
  question: string;
  selections: string[];
  text?: string | null;
}
/** The park was answered: what the agent asked, and what it was told.
 *
 *  The answer already reaches the model as the deferred call's own result, so this exists
 *  purely so the *transcript* can show it. Reading it back off that result would mean the
 *  client parsing prose the backend wrote, which is the backend's job and is done there
 *  (the same pairing feeds a cold load's projection), so warm and reloaded renders agree.
 *  Additive to v1. */
export interface QuestionAnswered extends Base {
  type: "question.answered";
  tool_call_id: string;
  answers: AnsweredQuestion[];
}
/** An action at the Auto permission level is being ruled on in the operator's place.
 *
 *  Announced *before* it is ruled on, so a review that takes a model call reads as work
 *  in progress rather than as a stalled turn. `summary` is the action's worst case as the
 *  backend extracted it — the same words the reviewer is judging, so the operator and the
 *  model are looking at one description rather than two.
 *
 *  `detail` is the act's own content where the tool has some worth reading — the task
 *  handed to a sub-agent, the words a skill is being rewritten with, the reason given for
 *  opening a credential. Null where there is none, which is most tools. It is the part of
 *  the call the reviewer ruled on beyond the summary, so the operator can read the same
 *  thing rather than a one-line paraphrase of it.
 *
 *  `reach` is how far a shell command *declared* it needs to go: the worktree, the
 *  network, or the operator's own machine. Null for every kind of act that declares
 *  nothing — a mail send, a file write — which is a different fact from declaring the
 *  widest reach and must not read the same. */
export interface ReviewStarted extends Base {
  type: "review.started";
  tool_call_id: string;
  name: string;
  summary: string;
  detail: string | null;
  reach: "workspace" | "network" | "host" | null;
}
/** How the review ruled, and on what.
 *
 *  `decision` is what the run then did: `allow` ran the call with no prompt, `ask` parked
 *  it for the operator anyway. `block` — refused outright — is no longer produced: an act
 *  the reviewer judged unrecoverable is put in front of the operator like anything else it
 *  will not clear. The word stays because it is already written into the stored events of
 *  threads reviewed before that changed. It deliberately carries more
 *  than the outcome — `stage` says whether the structural judge or a model settled it,
 *  `tier` on which ground when the judge did, `fenced` whether an OS fence was there to
 *  hold the command to what it declared, and the three axes say what the model saw.
 *  "Allowed" alone tells the operator nothing they can act on; "cleared structurally,
 *  fenced to the worktree" lets them tell an over-permissive rule from a well-judged call.
 *
 *  The axes are null when the model stage never ran — the judge cleared it, or there was
 *  nothing to review with — and `tier` is null in the mirror case, whenever the model
 *  settled it. */
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
}
/** Who a queued message is from. `operator` is somebody typing while a run is going;
 *  `subagent` is a report from one the agent launched, delivered into the thread that
 *  launched it; `agent` is one agent in the tree messaging another, named by `sender` on the
 *  queue frame. They ride the same road on purpose — a queued message is handed to the *next, not-yet-sent*
 *  request, so none can interrupt a model mid-stream — but they must not read the same,
 *  because a report shown as the operator's own words is a transcript lying about who said
 *  what. Absent on an older backend, which only ever sent the operator's.
 *
 *  `subagent` and `agent` both render as an incoming `subagent` row. */
export type MessageSource = "operator" | "subagent" | "agent";
/** A message arrived while the run was still executing; it is queued for injection
 *  at the run's next model-request boundary. `text` rides inline so a reattaching
 *  client rebuilds the pending bubble purely from replay. */
export interface MessageQueued extends Base {
  type: "message.queued";
  message_id: string;
  text: string;
  source?: MessageSource;
  /** The handle of the agent that sent it, when `source` is `agent`. */
  sender?: string | null;
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
/** The run has been asked to hold a queued message back, or to stop holding it.
 *
 *  Its own event rather than a field on `message.edited`, because the two are different
 *  facts with different lifetimes: an edit is a new text, a hold is a *state* the message
 *  sits in for as long as the operator has it open. It exists at all because the hold has
 *  to be the run's — the drain is server-side, so a client that merely declined to show
 *  the message as sendable would still watch it go. Replayed and broadcast like the rest,
 *  so a second tab sees the queue stop moving and why. */
export interface MessageHeld extends Base {
  type: "message.held";
  message_id: string;
  held: boolean;
}
/** A queued message was handed to the model (emitted in drain order); from here
 *  on it is part of the turn and persists as a normal user message. */
export interface MessageInjected extends Base {
  type: "message.injected";
  message_id: string;
  /** Repeated from the queue frame rather than looked up, so a client attaching after
   *  that frame scrolled out of its replay window still knows whose message landed. */
  source?: MessageSource;
}
/** One task on the agent's running list for this conversation. `blocked` only occurs
 *  when the backend enables subtasks/dependencies; it is carried here so a future flip
 *  of that switch is a rendering choice rather than a crash. */
export interface TaskItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled" | "blocked";
  /** Present-tense label for the task while it runs ("Reading the config"). */
  active_form?: string | null;
}
/** The agent's task list changed. Carries the whole list rather than a delta, so
 *  applying it is idempotent on replay and needs no ordering rules. */
export interface TasksUpdated extends Base {
  type: "tasks.updated";
  items: TaskItem[];
}
/** The written plan a plan-mode turn produced, or its status changing.
 *
 *  The other half of the pair above, and a different thing entirely: that one is the
 *  checklist the agent works through, this is the document the operator is being asked
 *  to approve. `revision` counts up on every resubmission, which is what lets the panel
 *  treat a revised plan as a new arrival rather than a redraw of the old one. */
export interface PlanUpdated extends Base {
  type: "plan.updated";
  title: string;
  body: string;
  steps: string[];
  status: PlanStatus;
  revision: number;
}
export type PlanStatus = "pending" | "approved" | "revising" | "denied";
/** The thread's permission level changed from inside the run — `plan_enter` narrowing
 *  it, or an approved plan raising it.
 *
 *  Load-bearing rather than informational. The client both holds the level and sends it
 *  back on every message, so without this it would write the stale one straight over a
 *  level the agent just moved. */
export interface PermissionChanged extends Base {
  type: "permission.changed";
  level: string;
  /** Why, in one line, for the transcript — "entering plan mode", "plan approved". */
  reason: string;
}
/* The `subagent.*` family is gone with the blocking delegation that emitted it. A
   sub-agent is a run of its own now — the panel reads its register row, its transcript
   and its own event stream, none of which a parent turn's stream could carry past the end
   of that turn. Mirrors backend/runs/events/. */
export interface LimitNotice extends Base {
  type: "limit.notice";
  /** "context" = the model's context window was exceeded; the run stops (it isn't
   *  silently degraded). "search" was the deep-research pipeline's two-empty-rounds
   *  abort — nothing emits it now that research is an ordinary thread, and it stays
   *  handled because the wire never narrows. Mirrors backend/runs/events/. */
  limit:
    | "steps"
    | "tool_calls"
    | "tokens"
    | "time"
    | "loop"
    | "verify"
    | "context"
    | "search";
  message: string;
  /** The stop marker this notice announces, when the bound also blocked the turn — the
   *  same string the turn persists as its `blocked_reason`. Two context stops share one
   *  `limit` value and do not share one remedy, so the toast reads the remedy off this
   *  rather than off prose. Optional on the wire: an older backend sends none. */
  detail?: string | null;
}

/** The exact `run.ended` detail (and persisted `blocked_reason`) a turn carries when the
 *  provider refused the request as too large and the chassis could not fold its way out.
 *  Exported from `agent/model_errors.py` on the backend and mirrored verbatim here,
 *  because it is the one stop the operator has a *specific* remedy for — the blocked
 *  turn's marker keys its "Compact and retry" control on this string, live and on
 *  reload. Any other stop reads as a plain bound and offers Continue alone. */
export const CONTEXT_OVERFLOW_DETAIL = "context window exceeded";

/** The same ceiling reached *after* the turn folded the thread and retried. Mirrored from
 *  `agent/model_errors.py` for the opposite reason to the constant above: it is the case
 *  where "Compact and retry" must **not** be offered — the fold already happened, and a
 *  button that re-sends the same oversized request is a button that lies. A turn carrying
 *  this reads as a plain stop, and the context toast drops its remedy sentence. */
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
  | ToolStarted
  | ToolProgress
  | ToolCompleted
  | ToolFailed
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
  | TasksUpdated
  | PlanUpdated
  | PermissionChanged
  | LimitNotice;

/** A run is over after one of these — the stream reader stops. */
export function isTerminal(event: RunEvent): boolean {
  return event.type === "run.ended" || event.type === "run.error";
}
