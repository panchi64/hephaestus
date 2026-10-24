/** Chat feature data contracts. Screens depend on these types; the mappers under
 *  `data/` translate backend responses and events into them. */

import type {
  CompactionReason,
  ContextWindow,
  SummarySection,
} from "~/lib/stream";

/** Why a fold happened, as the wire spells it. */
export type { CompactionReason };

/** The context-window state, computed by the backend (live over the run stream, or
 *  rebuilt on conversation load). The UI renders it and never computes it. */
export type ContextUsage = ContextWindow;

/** What the thread has cost so far — the stats panel behind the composer's `Stats`.
 *
 *  Cumulative over the conversation, not the last run. Every figure is the backend's
 *  (the live `run.metrics` frame and the conversation load's `stats` share this shape);
 *  nothing is derived in the UI.
 *
 *  `null` means unmeasured, not `0`: a provider with no cache figure, turns that predate
 *  the stopwatch, an endpoint that leaves token counts at zero. The strip omits a null
 *  segment rather than print a number that reads as a measurement. */
export interface ConversationStats {
  /** Completed operator exchanges on the active path. */
  turns: number;
  /** Model round-trips those turns took between them — always ≥ `turns`. */
  steps: number;
  toolCalls: number;
  /** Prompt and generation tokens summed across the path. */
  inputTokens: number | null;
  outputTokens: number | null;
  /** Prompt tokens served from the provider's cache, 0–1. Provider-reported, so
   *  null on the many endpoints that don't send one. */
  cacheHitRatio: number | null;
  /** Wall-clock the model was working, and the tools were running, in ms. */
  llmMs: number | null;
  toolMs: number | null;
  /** Mean time to first content, in ms, over the responses that produced any. */
  ttftAvgMs: number | null;
  /** Generation throughput, output tokens per second of model time. */
  tokensPerSecond: number | null;
  /** The last model request on its own: its route and what the provider's cache did.
   *  Separate from the totals because a sum hides a fallback model and a cold cache.
   *  Null before the first response. */
  lastRequest: LastRequest | null;
}

/** One request's own figures. Every token field is null when the provider reported
 *  nothing, never 0. */
export interface LastRequest {
  /** The model that actually answered, provider-prefixed when reported
   *  (`openai:qwen3-32b`). Not necessarily the model the thread is bound to. */
  route: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
}

/** "compaction" is the chassis marking where earlier turns were folded into a summary:
 *  a full-width divider, not a bubble, carrying the summary in `content`.
 *
 *  "subagent" is a report from a sub-agent the agent launched, shown as a report rather
 *  than a bubble because the operator did not write it and is not replying to it.
 *
 *  Both carry their text in `content` and have no actions. */
export type Role = "user" | "assistant" | "compaction" | "subagent" | "notice";

/** One typed slice of a payload the backend wrapped. `untrusted` came from outside and
 *  was fenced for the model (`source` is the fence's attribute); `context_file` is a
 *  project instruction file (`path`). */
export interface ContentPart {
  kind: "text" | "untrusted" | "context_file";
  text: string;
  source?: string;
  path?: string;
}

export type ToolStatus = "preparing" | "running" | "ok" | "error";

/** A call still out: its arguments are streaming or it is executing. */
export function isLiveStatus(status: ToolStatus): boolean {
  return status === "preparing" || status === "running";
}

export interface ToolInvocation {
  id: string;
  /** Namespaced tool name, e.g. "memory.recall". */
  name: string;
  /** Human-readable summary of every argument, shown when the card is expanded. */
  args: string;
  /** The one argument that says what the call is about (path, query, command), for the
   *  collapsed row. Undefined when none stands out; the row shows `args` instead. */
  detail?: string;
  /** Why the model made this call, in its own words (the `narration` argument the
   *  backend injects into every tool). Takes the collapsed row's detail slot when
   *  present: it says what the call was *for*. Absent for read-only calls and for a
   *  model that skipped the optional argument; `detail` is then the fallback. */
  narration?: string;
  status: ToolStatus;
  /** Characters of arguments streamed so far while `status='preparing'`. */
  argsChars?: number;
  /** Latest progress note while `status='running'` (`tool.progress`), e.g. the sandbox
   *  spinning up. */
  progress?: string;
  /** What came back in a few characters ("12 entries", "exit 0"); undefined when
   *  the result has no shape worth summarizing. */
  outcome?: string;
  /** Result preview shown when expanded. */
  result?: string;
  /** The result split into typed parts, when the backend sent them. `result` stays the
   *  raw text for copy and as the fallback. */
  resultParts?: ContentPart[];
  /** Error detail shown when status='error'. */
  error?: string;
  /** The failure was the platform's own fault, not the call's or the environment's.
   *  The model was told and carried on; the card marks it so the bug stays visible. */
  platform?: boolean;
  /** How long the call took: `tool.progress` elapsed seconds while out, then the result's
   *  own measurement. Never a client stopwatch, which would time the stream. */
  elapsedMs?: number;
  /** What the call saw (a browser screenshot). The backend routes pixels here rather than
   *  letting the wire's user-role carrier surface as a turn the operator never took. */
  images?: ToolImage[];
  /** For a command-running call that is not a terminal, such as a backgrounded shell
   *  process whose result is a handle. Absent for nearly every tool. */
  boundary?: CommandBoundaryFacts;
  /** The program a call ran, for a tool whose argument is code (`run_code`). Separate
   *  from `args` so the open card shows it as code. */
  script?: string;
  /** The calls a script made, in start order. Only `run_code` has them; each keeps its
   *  own lifecycle and nests here instead of sitting on the turn's rail. */
  children?: ToolInvocation[];
}

/** One image a tool returned: base64 payload plus its media type. The scheme is the
 *  renderer's to add, so nothing stores a data URI it would have to re-parse. */
export interface ToolImage {
  mediaType: string;
  data: string;
}

/** A sensitive action the agent paused to ask about (`approval.required`). The
 *  operator approves or denies; the run resumes on the same stream. */
export interface Approval extends ApprovalCautions {
  toolCallId: string;
  name: string;
  /** Full call arguments, shown so the operator can judge the action. */
  args: Record<string, unknown>;
  /** One-line summary of what will happen. */
  summary: string;
  /** Longer plain-language explanation, when the tool provides one. */
  explanation?: string;
  /** What a review made of this call, copied off the matching `review.completed`. Present
   *  only at the Auto level. `too_destructive` is carried so the reviewer's warning about
   *  an irreversible act reaches the card where the operator is deciding. */
  risk?: "low" | "high" | "too_destructive";
  /** The review's account of its verdict: the axes it scored and, when a standing grant
   *  supplied the authorization, that it did. */
  reviewReason?: string;
  /** True once a decision for this approval 409'd: the run had already resumed elsewhere
   *  (a second tab, a retried request). Non-interactive; a refetch reconciles the
   *  transcript with the winning decision. */
  stale?: boolean;
}

/** One answer the agent offered for a question. */
export interface QuestionOption {
  label: string;
  /** One short line on what choosing this would mean, when the agent supplied one. */
  description?: string;
}

/** One question inside a parked `ask_user` call. */
export interface QuestionSpec {
  question: string;
  options: QuestionOption[];
  /** Whether several options may be chosen together. Writing an answer instead is
   *  always allowed, so it is not a mode and not a flag. */
  multiSelect: boolean;
}

/** What the operator said to one question, as the transcript reads it back. The backend
 *  pairs the question text from the parked call's own arguments, never from the client,
 *  so a card cannot restate a question as something other than what was asked. */
export interface AnsweredQuestion {
  question: string;
  selections: string[];
  text?: string;
}

/** Whether a parked call has been answered. Tests length, not truthiness: a malformed
 *  `ask_user` can parse to no questions, and its empty reply list is not an answer. */
export const isAnswered = (question: Question): boolean =>
  (question.answers?.length ?? 0) > 0;

/** A parked `ask_user` call, answered in one submit: the run resumes on the call, not on
 *  a question. */
export interface Question {
  toolCallId: string;
  questions: QuestionSpec[];
  /** Same meaning and reconciliation as `Approval.stale`. */
  stale?: boolean;
  /** One entry per question, positionally. Present means answered: the call stops being
   *  a park in the dock and becomes a transcript row. */
  answers?: AnsweredQuestion[];
}

/** Lifecycle of a host-machine command (`run_host_command`). */
export type HostCommandPhase =
  | "pending" // awaiting the operator's approval
  | "running" // approved; executing on the host
  | "ok" // finished, exit 0
  | "error" // finished non-zero, or the launch failed
  | "denied" // the operator refused it
  | "stale"; // a decision 409'd — already resolved elsewhere (see `Approval.stale`)

/** How far a command declared it needs to reach: the VM with no network, the VM with the
 *  allowed domains, or the operator's own machine. Checked against every path the
 *  command names before anything runs. */
export type CommandReach = "offline" | "network" | "host";

/** What a command-running call declared and what held it to that. Shared by the
 *  foreground terminal and by backgrounded shell calls, which render as ordinary tool
 *  cards, so one component renders both. */
export interface CommandBoundaryFacts {
  reach?: CommandReach;
  /** Whether an OS fence built from the declaration was applied. Not implied by `reach`:
   *  a host with no sandbox primitive has no fence to build. */
  fenced?: boolean;
  /** Why it ran unfenced; present only when `fenced` is false. */
  unfencedReason?: string;
  /** Whether a VM command ran with the network, which an `offline` declaration can
   *  differ from. Absent for a host command and on older results. */
  network?: boolean;
  /** A permission failure in the output caused by the fence, in words, so a denied write
   *  reads as the fence holding rather than a broken command. */
  fenceNote?: string;
  /** The host the gateway refused a VM command, when it named one; the operator can allow
   *  it for this thread. */
  deniedHost?: string;
}

/** What made an approval ask on every call, and what makes it riskier than usual: the
 *  backend's own sentences, verbatim. */
export interface ApprovalCautions {
  /** Why this call always asks. Set, it is once-only: no standing approval is offered. */
  guard?: string;
  /** Set on a host command about to run with no fence in force, saying why. */
  unconfined?: string;
}

/** A shell command rendered as a persistent terminal: the command, the approval gate
 *  and, once it runs, its captured output. Folded from `tool.started` /
 *  `approval.required` / `tool.completed` (warm) or the persisted tool call (cold),
 *  keyed by `toolCallId`. */
export interface HostCommand extends CommandBoundaryFacts, ApprovalCautions {
  toolCallId: string;
  /** The namespaced tool that asked. More than one tool renders as a terminal and a
   *  conversation grant is recorded per tool name, so the card cannot assume which. */
  name: string;
  /** The exact command line the agent asked to run. */
  command: string;
  /** Plain-language description of the effect, shown for the approval decision. */
  explanation?: string;
  /** Same two review fields as `Approval`; `too_destructive` mostly shows up on shell
   *  commands (`git commit --amend`, `rm -rf build`). */
  risk?: "low" | "high" | "too_destructive";
  reviewReason?: string;
  phase: HostCommandPhase;
  /** Captured output, present once the command has run. `exitCode` null means it timed
   *  out (never exited), which differs from not having run (undefined). */
  exitCode?: number | null;
  stdout?: string;
  stderr?: string;
  timedOut?: boolean;
  /** A short failure hint, or a launch error. */
  error?: string;
  /** Wall clock around the spawn, from the result's own measurement. */
  elapsedMs?: number;
  /** Output appended from `tool.progress` deltas while `phase` is `running`.
   *
   *  One string where the settled command has two: mid-flight the wire interleaves stdout
   *  and stderr with no field to tell them apart. The card prefers `stdout`/`stderr` once
   *  the call completes. */
  streamed?: string;
}

/** How a View version previews on stage: a captured static file rendered by kind,
 *  with its bytes at `/views/{artifactId}/content`. Absent ⇒ a live/auto preview
 *  (a running head, or the frontend auto-picks an entry HTML page from the tree). */
export interface ViewPreviewRef {
  kind: "html" | "image" | "text" | "other";
  artifactId: string;
}

/** The live, interactive head of the View — a running server (`view.live`). */
export interface ViewLiveRef {
  url: string;
  title?: string;
}

/** A version of the conversation's View (`view.snapshot`), minted by a `show`: a
 *  point-in-time capture of the agent's sandbox tree (browsed and diffed via
 *  `/views/snapshots/{id}/…`) plus how it previews. Conversation-scoped; the inline chip
 *  references it by id. `summary` is a change tally, e.g. `"+2 ~1 -0"`. */
export interface ViewSnapshotRef {
  snapshotId: string;
  title?: string;
  createdAt: string;
  filesChanged: number;
  summary: string;
  /** The static preview the version was shown with, or null for a live/auto preview. */
  preview: ViewPreviewRef | null;
  /** Whether the operator pinned this version (`POST /views/snapshots/{id}/keeper`). */
  keeper?: boolean;
}

/** One file in a workspace snapshot's tree, with its change status vs. the prior
 *  snapshot. */
export interface SnapshotFile {
  path: string;
  status: "added" | "modified" | "unchanged";
}

/** A file's unified diff within a snapshot. `diff` is empty for binary files or
 *  files that didn't change. */
export interface SnapshotDiff {
  path: string;
  status: "added" | "modified" | "removed";
  diff: string;
}

/** One renderable unit of an assistant turn. A turn is an ordered list of these in the
 *  agent's emission sequence. Thinking and text deltas fold onto a trailing block of the
 *  same kind; a kind change starts a new block. `id` is stable for keyed rendering. */
export type AssistantBlock =
  | ThinkingBlock
  | TextBlock
  | ToolBlock
  | ContextBlock
  | ReviewBlock
  | HostCommandBlock
  | ApprovalBlock
  | QuestionBlock
  | ViewVersionBlock
  | ViewLiveBlock;

export type BlockKind = AssistantBlock["kind"];

/** A private reasoning passage (`thinking.delta`). */
export interface ThinkingBlock {
  kind: "thinking";
  id: string;
  text: string;
}
/** A passage of the answer the operator reads (`answer.delta`). */
export interface TextBlock {
  kind: "text";
  id: string;
  text: string;
}
/** A single generic tool invocation, rendered as a call card. */
export interface ToolBlock {
  kind: "tool";
  id: string;
  tool: ToolInvocation;
}
/** Text the chassis put in front of the model that nobody in the thread wrote: project
 *  instruction files, the skill catalog, the plan reminder, the date (`context.injected`).
 *
 *  It sits on the rail in sequence but must read as not the model's work;
 *  `ContextInjectionCard` carries that distinction. */
export interface ContextBlock {
  kind: "context";
  id: string;
  injection: ContextInjection;
}

/** One injected block: which contributor put it there, where in the request it landed,
 *  what it cost, and what it said. `truncated` marks a `text` the wire capped — the
 *  `tokens` figure is always over the whole block. */
export interface ContextInjection {
  contributor: string;
  placement: "instructions" | "prompt";
  tokens: number;
  text: string;
  truncated: boolean;
}

/** One fold in flight: its trigger, roughly what is going into it, the summary as it is
 *  written, and whether it has landed.
 *
 *  Rides on a compaction turn (`ChatMessage.compaction`), not on the assistant's rail: a
 *  hand-started fold has no assistant turn, and a rail row would duplicate the divider
 *  it settles into. */
export interface CompactionProgress {
  reason: CompactionReason;
  /** Messages about to be folded. */
  messages: number;
  /** Char-based estimate of their cost, the same proxy the gauge uses. */
  tokensEstimate: number;
  /** The summary as the model writes it, accumulated from `compaction.delta`.
   *
   *  Unstripped, unanchored and unfenced: display only, discarded when the fold settles.
   *  The operator keeps the parsed summary on the compaction turn. */
  summary?: string;
  done?: boolean;
}

/** An action the chassis ruled on in the operator's place at the Auto level
 *  (`review.started` / `review.completed`), shown immediately ahead of the call it
 *  judged. Without it, a call the operator never approved is indistinguishable from a
 *  gate that failed open. */
export interface ReviewBlock {
  kind: "review";
  id: string;
  review: Review;
}

/** One review: the action's worst case, how it was ruled on and, when the model rather
 *  than the deterministic judge ruled, the three axes it scored.
 *
 *  `decision` is undefined only while the review is in flight; the row appears when the
 *  review starts so a model-call pause reads as "being checked", not a stall. */
export interface Review {
  toolCallId: string;
  /** The namespaced tool the model asked for. */
  name: string;
  /** The action's worst case, in the words the reviewer judged. */
  summary: string;
  /** The act's own content where worth reading (a delegated task, a skill rewrite, a
   *  credential-open reason), so the decision is reviewable on the material the reviewer
   *  saw. Undefined for most tools. */
  detail?: string;
  /** How far a shell command said it needs to go. Undefined means it declared nothing,
   *  not the widest reach. */
  reach?: CommandReach;
  /** Whether the command will run with the network; an install or a named address gets
   *  it despite an `offline` declaration. */
  network?: boolean;
  decision?: "allow" | "ask" | "block";
  /** Which stage settled it: the structural judge, or the model. */
  stage?: "judge" | "reviewer";
  /** On which ground the judge cleared it, when it did. */
  tier?: "read" | "sandbox" | "workspace";
  /** Whether an OS fence was available to hold the command to its declaration. False is
   *  why a contained command reached a reviewer, and the operator can fix it, so it is
   *  shown rather than inferred from a missing tier. */
  fenced?: boolean;
  reason?: string;
  risk?: "low" | "high" | "too_destructive";
  authorization?: "explicitly_no" | "neutral" | "explicitly_yes";
  correctness?: string;
  /** The decision model's probabilities behind the axes, by question. */
  scores?: Record<string, number>;
  /** Set when the call needed the decision model and none could answer. */
  setup?: "unbound" | "unreachable";
}

/** A host-machine command, rendered as a persistent terminal. */
export interface HostCommandBlock {
  kind: "host_command";
  id: string;
  command: HostCommand;
}
/** A sensitive action paused for the operator's decision.
 *
 *  Folded into the turn but not rendered on the rail: the dock that takes over the
 *  composer (`ParkDock`) answers it and reads its pending set from these blocks, which
 *  the transcript grouping filters out (`blocks.ts`). The tool call itself stays behind. */
export interface ApprovalBlock {
  kind: "approval";
  id: string;
  approval: Approval;
}
/** A question paused for the operator's answer; rendered in the dock like `ApprovalBlock`. */
export interface QuestionBlock {
  kind: "question";
  id: string;
  question: Question;
}
/** An inline chip for a version the agent `show`ed during the turn; opens it in the
 *  viewport. References the conversation-scoped `ViewSnapshotRef` by id. */
export interface ViewVersionBlock {
  kind: "view_version";
  id: string;
  snapshotId: string;
  title?: string;
  /** The preview kind, for the chip icon (null/absent ⇒ a live/auto preview). */
  previewKind?: "html" | "image" | "text" | "other" | null;
}
/** The View's live head, started during the turn; rendered as a LIVE chip. */
export interface ViewLiveBlock {
  kind: "view_live";
  id: string;
  live: ViewLiveRef;
}
/** A source the turn's `web_search`/`web_fetch` calls surfaced (`citation.added`),
 *  rendered in a deduped Sources row beneath the answer; its display number is its
 *  position there. */
export interface Citation {
  /** Null for a corpus passage, which has a locator, not an address; read `kind` before
   *  rendering a link. */
  url: string | null;
  title?: string;
  /** Backend-supplied identity that repeat sightings fold by (web sources and corpus
   *  passages fold by different fields). Optional only on the cold path. */
  key?: string;
  kind?: "web" | "corpus";
  /** `listed` (returned by a search, never opened) < `read` (text reached the model) <
   *  `cited`. A source may be emitted several times as it climbs; the highest wins. */
  engagement?: "listed" | "read" | "cited";
  snippet?: string;
  /** The provider's own date string, unparsed and in any format. */
  published?: string;
  retrievedAt?: string;
  sourceId?: string;
  ref?: string;
}

/** Folds several sightings of one source. Mirrors `core/citations.ENGAGEMENT_ORDER`,
 *  which owns the vocabulary. */
export const ENGAGEMENT_ORDER: Record<
  NonNullable<Citation["engagement"]>,
  number
> = { listed: 0, read: 1, cited: 2 };

export interface ChatMessage {
  id: string;
  role: Role;
  /** `subagent` rows: the agent that sent the message, when it is one agent messaging
   *  another and not a report. */
  author?: string;
  /** `subagent` rows that are a report: the agent that reported. */
  reporter?: string;
  /** User turns: the operator's prompt. Assistant turns: "" (the answer lives in the
   *  `text` blocks) so copy/edit/version paths stay role-agnostic. */
  content: string;
  /** User turns: non-text parts split out of the bubble (attachment text, a routine's
   *  previous run) that `content` no longer holds. */
  parts?: ContentPart[];
  /** `notice` rows: the kind (`open-tasks`); `content` is the notice body. */
  notice?: string;
  /** Assistant turns: the ordered block sequence, the single source of truth for what
   *  the turn rendered. */
  blocks?: AssistantBlock[];
  /** Assistant turns: sources surfaced by `web_search`/`web_fetch`, in citation order. */
  citations?: Citation[];
  /** The run ended with `outcome: "blocked"` (a usage/loop/context/time bound); shown as
   *  a persistent inline notice, not only the transient `limit.notice` toast.
   *  `blockedDetail` is the human-readable reason. */
  blocked?: boolean;
  blockedDetail?: string;
  /** The run this assistant turn streams from, needed to approve or cancel it. */
  runId?: string;
  /** True while tokens are still streaming in. */
  streaming?: boolean;
  /** True from submit until the run's first event: `run.started` is emitted only after
   *  the concurrency semaphore clears. Shown as "queued" so the wait is not read as a
   *  stalled model. */
  queued?: boolean;
  /** The SSE transport exhausted its reconnect budget without a terminal event, so the
   *  run may still be alive server-side. Neither streaming nor settled; it gets its own
   *  re-attach affordance. */
  detached?: boolean;
  createdAt: string;
  /** Model/endpoint that produced an assistant message. */
  model?: string;
  /** 0-based position among sibling versions (branches). */
  versionIndex?: number;
  /** Total sibling versions (≥1); >1 means it can be cycled. */
  versionCount?: number;
  pinned?: boolean;
  /** User turns: ids of attached uploads, shown as read-only chips. */
  attachmentIds?: string[];
  /** User turns: workspace-relative paths named with `@`, shown as read-only chips. */
  fileRefs?: string[];
  /** Compaction dividers only. `foldedMessages` counts messages, not exchanges (a plain
   *  exchange is two, a tool-heavy turn many), so it is not called turns: the backend
   *  does not count turns at fold time. `tokensBefore` estimates the folded messages and
   *  `tokensAfter` the summary replacing them, not the next turn's footprint. Both are
   *  char-based proxies, hence the `~` on screen.
   *
   *  The backend always sends all three (0 for nothing), so guard with `> 0`, not
   *  presence. */
  foldedMessages?: number;
  tokensBefore?: number;
  tokensAfter?: number;
  /** Compaction dividers only: what triggered the fold, live (`conversation.compacted`)
   *  and cold (stored on the checkpoint). A checkpoint folded before reasons were
   *  recorded has none, so the divider shows it as an extra segment. */
  compactionReason?: CompactionReason;
  /** Compaction dividers only: the summary split into sections by the backend. `content`
   *  stays the stored text; the divider falls back to it when this is empty.
   *
   *  Parsed backend-side because the section roster is a security boundary: a heading
   *  quoted from a fetched page must not open a section and carry what follows out of the
   *  untrusted fence. Live and cold reads call the same function. */
  summarySections?: SummarySection[];
  /** Compaction turns only, and only while the fold is in flight: what it is doing and
   *  the summary as the model writes it. The turn exists from the fold's start; when
   *  `conversation.compacted` lands it is dropped and the settled one is seated at the
   *  fold boundary (`foldAnchor.ts`). Absent on settled compaction turns. */
  compaction?: CompactionProgress;
}

/** The in-flight run driving a conversation, present on a cold read taken mid-stream so
 *  the client can reattach and replay what it missed. */
export interface ActiveRun {
  id: string;
  /** What the run is: a chat turn, or a fold, which has no assistant turn to seed. */
  kind: string;
  status: string;
  lastSeq: number;
  /** The persisted prompt row this run is answering; the streaming answer goes directly
   *  after it. Null for a fold, and briefly before a chat run has recorded one. */
  promptMessageId: string | null;
}

export interface ChatSession {
  id: string;
  title: string;
  /** No stored title yet, so `title` is the derived fallback. */
  untitled: boolean;
  messages: ChatMessage[];
  /** Context-window state rebuilt from the last turn, or null. Seeds the header meter. */
  context: ContextUsage | null;
  /** The cumulative readout rebuilt by the backend; null for a thread that never ran.
   *  Seeds the composer's readout so it matches the live totals. */
  stats: ConversationStats | null;
  /** Set only while a turn is still streaming server-side. */
  activeRun: ActiveRun | null;
  /** Workspace snapshots across the thread (newest last), seeding the viewport history. */
  snapshots: ViewSnapshotRef[];
  /** Seeded on load so the composer's control returns where the operator left it. */
  permission: PermissionLevel;
}

/** The backend's status for a live run, derived from the run registry.
 *  `awaiting_input` is parked on the operator's approval; the rest are in-flight work. */
export type ChatActivity = "queued" | "running" | "awaiting_input";

/** How the most recent terminal run for a thread ended.
 *
 *  Absent means "nothing known", never "nothing happened": the backend derives it from a
 *  bounded in-memory run registry, so a restart or enough traffic erases it. Do not read
 *  absence as idle, unfinished, or never run. */
export type ChatOutcome = "done" | "error" | "blocked" | "cancelled";

export interface ChatSummary {
  id: string;
  title: string;
  updatedAt: string;
  /** When the thread was opened (ISO-8601), the session clock's input. Unlike
   *  `updatedAt` it does not move with each turn. */
  createdAt: string;
  messageCount: number;
  /** Last-message snippet for preview cards. */
  preview?: string;
  /** Two or three sentences from the backend's utility model on what the agent did,
   *  written once the thread has idled; replaced on each regeneration. Absent until the
   *  sweep has run (normal for a thread still in use), so render without it. */
  workSummary?: string;
  /** The model of the conversation's most recent answer. */
  model?: string;
  /** Set while a run drives this thread. */
  activity?: ChatActivity;
  /** How the last terminal run ended, when remembered. Read only at rest: `activity`
   *  takes precedence and both can be set (a finished thread sent to again). */
  lastOutcome?: ChatOutcome;
  /** The basename of the project directory the thread is filed under; absent when
   *  unfiled or the project was deleted. Never a path. */
  workspace?: string;
  /** The project the thread is scoped to. The rail groups by this, not by `workspace`:
   *  two directories can share a basename, and it lists directories with no threads. */
  projectId?: string;
  /** The custom agent the thread runs as. Absent for the default agent. */
  agentName?: string;
  /** The thread this one was opened from, when it belongs to an agent tree. */
  parentId?: string;
}

/** How far the model may go in a thread (the backend's `services/permissions`
 *  vocabulary). Persisted on the thread and changed by sending at a different level. */
export type PermissionLevel = "plan" | "manual" | "edit" | "auto" | "yolo";

export interface PermissionLevelSpec {
  id: PermissionLevel;
  /** Sentence case, in plain words ("Ask first"); the id is backend vocabulary the
   *  operator should not need to know. */
  label: string;
  /** What the model may do, as the operator would say it. */
  description: string;
}

/** The five levels, ordered by how much latitude they give. */
export const PERMISSION_LEVELS: readonly PermissionLevelSpec[] = [
  {
    id: "plan",
    label: "Plan only",
    description: "Read only. Ends in a plan for you to approve.",
  },
  {
    id: "manual",
    label: "Ask first",
    description: "Asks before anything that isn't reading.",
  },
  {
    id: "edit",
    label: "Workspace",
    description: "Works in its own workspace; asks to reach past it.",
  },
  {
    id: "auto",
    label: "Auto-review",
    description:
      "Acts on its own inside the worktree, reviewed beyond it, asks only on doubt.",
  },
  // The one description that is a warning, not a summary: the last thing an operator
  // reads before choosing it.
  {
    id: "yolo",
    label: "No limits",
    description:
      "Free rein. Nothing stops for you — not commands, mail, credentials or anything it can't undo.",
  },
];

/** The level a free-typed word names (id or label, any case), or null. For `/level` in
 *  the composer. The label must work as well as the id because the control shows only
 *  labels. The backend re-validates the resulting id. */
export function parsePermissionLevel(value: string): PermissionLevel | null {
  const word = value.trim().toLowerCase();
  return (
    PERMISSION_LEVELS.find(
      (spec) => spec.id === word || spec.label.toLowerCase() === word,
    )?.id ?? null
  );
}

/** The level after `level` for the composer's Shift+Tab cycle.
 *
 *  Never lands on `yolo`: it asks nothing, so it is reachable only by naming it. From
 *  `yolo` the cycle wraps to the strictest level. */
export function nextPermissionLevel(level: PermissionLevel): PermissionLevel {
  const cycle = PERMISSION_LEVELS.filter((spec) => spec.id !== "yolo");
  const at = cycle.findIndex((spec) => spec.id === level);
  // `-1` (yolo) wraps to the head of the list.
  return cycle[(at + 1) % cycle.length]!.id;
}

/** The level a new thread starts at: the backend's `DEFAULT_PERMISSION`. Existing
 *  threads keep their stored level. Auto reaches no further than Edit; it differs in who
 *  answers at the boundary. */
export const DEFAULT_PERMISSION_LEVEL: PermissionLevel = "auto";

/** The level that does the least (the backend's `STRICTEST_PERMISSION`), where every
 *  not-yet-known reading lands. A level rides every send and the backend persists it, so
 *  an unknown one must not widen a thread the operator already narrowed. */
export const STRICTEST_PERMISSION_LEVEL: PermissionLevel = "plan";

/** Whatever the wire said, as a level this build has a rule for.
 *
 *  Nothing means a fresh thread (or an older backend) and gets the default. A present but
 *  unreadable value degrades to the strictest level, as the backend does; conflating the
 *  two would make every new thread read-only or let an unknown string widen access. */
export function permissionLevel(value: string | undefined): PermissionLevel {
  if (!value) return DEFAULT_PERMISSION_LEVEL;
  return PERMISSION_LEVELS.some((spec) => spec.id === value)
    ? (value as PermissionLevel)
    : STRICTEST_PERMISSION_LEVEL;
}

/** What the operator said to one question: the labels chosen exactly as offered, and any
 *  text written instead of or as well. The server checks both against the parked call
 *  and renders the prose itself. */
export interface QuestionReply {
  selections: string[];
  text?: string;
}

/** One parked `ask_user` call, answered. `replies` is positional, one per question. */
export interface QuestionAnswer {
  tool_call_id: string;
  replies: QuestionReply[];
}

/** One decision in an approval response (mirrors the backend's shape). */
export interface ApprovalDecision {
  tool_call_id: string;
  approved: boolean;
  message?: string;
  override_args?: Record<string, unknown>;
  /** How long a yes lasts. "once" (default) is this call. "conversation" also records a
   *  grant so the same act isn't re-prompted for the thread (for a command tool, the
   *  command, derived server-side from the parked call). "conversation_tool" grants the
   *  whole tool, a materially larger statement. Acted on only when `approved` is true. */
  scope?: "once" | "conversation" | "conversation_tool";
  /** Which kind of no, read only when `approved` is false. Both stop the call; they differ
   *  in what the model is told: "deny" (default) is a refusal, "revise" asks for a
   *  different version of the same act (mainly for a submitted plan). */
  intent?: "deny" | "revise";
}

/** The written plan a plan-mode turn submitted: the document, not the checklist.
 *  `revision` counts resubmissions, so the panel can tell a revised plan from a
 *  re-render. */
export interface PlanDocument {
  title: string;
  body: string;
  steps: string[];
  status: PlanStatus;
  revision: number;
}

/** Where a plan stands. `pending` is the only one waiting on the operator; `revising`
 *  means they asked for changes and the agent is writing the next version. */
export type PlanStatus = "pending" | "approved" | "revising" | "denied";

/** A live conversation-scoped auto-approval grant: the operator's visible, revocable
 *  record of what auto-approves for the rest of the thread. The TTL stays backend-side. */
export interface ApprovalGrant {
  /** The namespaced tool name that auto-approves, e.g. "corpus_retrieve". */
  toolName: string;
  /** The command scope as words (`["uv", "run", "pytest"]`); empty for the whole tool.
   *  Tells apart several grants on one tool, in the chip and the revoke. Kept as words
   *  because the revoke sends it straight back; joining is for the chip label. */
  commandPrefix: string[];
  /** The operator's wider pick for the whole tool, which the reviewing level takes as
   *  the authorization rather than weighing. Needed because an empty `commandPrefix`
   *  cannot distinguish it from a non-command tool's grant or a scheduled task's seed. */
  decisive: boolean;
  /** Whether this tool's grants are scoped to a command at all; otherwise "any command"
   *  is not a contrast and names commands that do not exist. */
  commandScoped: boolean;
}

/** A conversation's compaction state, same shape for both reductions. `override` is the
 *  stored per-chat choice (`null` inherits the global setting); `effective` is the
 *  backend-resolved on/off the UI renders. */
export interface CompactionState {
  override: boolean | null;
  effective: boolean;
}
