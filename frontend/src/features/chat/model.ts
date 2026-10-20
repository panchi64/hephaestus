/** Chat feature data contracts. This is the SEAM: screens depend on these
 *  types, and the mappers under `data/` — `messages.ts`, `hostCommands.ts`,
 *  `summaries.ts`, `viewSnapshots.ts`, `citations.ts` — map backend
 *  responses/events to them, so screens don't change when the mapping behind
 *  them does. */

import type {
  CompactionReason,
  ContextWindow,
  SummarySection,
} from "~/lib/stream";

/** Why a fold happened, as the wire spells it. Re-exported so chat code reads its seam
 *  types from one place. */
export type { CompactionReason };

/** The context-window state, derived and emitted by the backend (live over the
 *  run stream, or reconstructed on conversation load). Purely a carrier — the
 *  UI renders it, it does not compute it. */
export type ContextUsage = ContextWindow;

/** What the thread has cost so far — the stats panel behind the composer's `Stats`.
 *
 *  **Cumulative over the conversation, not the last run**, and every figure is the
 *  backend's: it counts the active path and measures its own wall-clock, and this is
 *  a carrier for the result. Nothing here is derived in the UI — not the averages,
 *  not the rates, not the ratio. Two sources fill it with the identical shape (the
 *  live `run.metrics` frame and the conversation load's `stats`), so a reload changes
 *  nothing about what the line says.
 *
 *  **`null` means unmeasured, and is not the same as `0`.** A provider that reports
 *  no cache figure, a thread whose turns predate the stopwatch, an endpoint that
 *  leaves token counts at zero — all report null, and the strip omits that segment
 *  rather than printing a number that would read as a measurement. */
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
  /** The last model request on its own — the route it took and what the provider's
   *  cache did with it. Null on a thread that has never produced a response. Kept
   *  apart from the cumulative figures above because a running total cannot answer
   *  either question: a fallback chain's second model and a cold cache both vanish
   *  into a sum. */
  lastRequest: LastRequest | null;
}

/** One request's own figures — see `ConversationStats.lastRequest`. Every token field
 *  is null when the provider reported nothing, never 0. */
export interface LastRequest {
  /** The model that actually answered, prefixed by its provider when one is reported
   *  (`openai:qwen3-32b`). Not necessarily the model the thread is bound to. */
  route: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
}

/** "compaction" is not a turn either party took — it is the chassis marking where the
 *  thread's earlier turns were folded into a summary. Rendered as a full-width divider,
 *  not a bubble; it carries the summary in `content` and has no actions.
 *
 *  "subagent" is the other turn nobody in the room took: a sub-agent the agent launched
 *  has reported back, and that report reached the thread in the one shape a model has for
 *  a message from outside itself. Shown as a report rather than as a bubble, because the
 *  operator has not seen it, did not write it, and is not being replied to. Like a
 *  compaction it carries its text in `content` and has no actions — there is nothing to
 *  edit, and nothing to send it back to. */
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
  /** The one argument that says what this call is *about* — the path, the query,
   *  the command — for the collapsed row. Undefined when no argument stands out,
   *  in which case the row shows `args` instead. */
  detail?: string;
  /** Why the model made this call, in its own words — the `narration` argument the
   *  backend injects into every tool.
   *
   *  It takes the collapsed row's detail slot when present, because it answers a
   *  question the argument never could: `Read agent.py` says what happened and
   *  "Checking how the run loop handles a cancel" says what it was *for*, which is
   *  the thing an operator reading a turn back is actually after. Absent for a
   *  read-only call (never asked for) and for a model that declined to write one
   *  (the argument is optional on purpose), so the detail remains the fallback. */
  narration?: string;
  status: ToolStatus;
  /** Characters of arguments streamed so far while `status='preparing'`. */
  argsChars?: number;
  /** Latest progress note while `status='running'` (`tool.progress`), e.g. the
   *  sandbox spinning up. Reassures the operator the wait is work, not a stall. */
  progress?: string;
  /** What came back, in a few characters — "12 entries", "exit 0". Set on
   *  completion; undefined when the result has no shape worth summarizing. */
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
  /** How long the call took. From the run while it is out (`tool.progress` carries the
   *  elapsed seconds), then from the result's own measurement for a tool that reports
   *  one — never a stopwatch started here, which would be timing the stream. */
  elapsedMs?: number;
  /** What the call *saw* — a browser screenshot, today. A tool that hands back pixels
   *  hands them back for the model, and the backend routes them here rather than
   *  letting the wire's user-role carrier surface as a turn the operator never took. */
  images?: ToolImage[];
  /** For a call that ran a command without being a terminal — a backgrounded shell
   *  process, whose result is a handle rather than output. Absent for everything else,
   *  which is nearly every tool: nothing else declares a reach. */
  boundary?: CommandBoundaryFacts;
  /** The script a call ran, for a tool whose argument *is* a program (`run_code`).
   *  Kept apart from `args` so the open card can show it as code rather than as one
   *  `code=…` line in the argument dump. */
  script?: string;
  /** The calls a script made, in the order they started. Only a `run_code` call has
   *  them; each is a call of its own with its own lifecycle, nested here rather than
   *  sitting on the turn's rail beside the call that made it. */
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
export interface Approval {
  toolCallId: string;
  name: string;
  /** Full call arguments, shown so the operator can judge the action. */
  args: Record<string, unknown>;
  /** One-line summary of what will happen. */
  summary: string;
  /** Longer plain-language explanation, when the tool provides one. */
  explanation?: string;
  /** What a review made of this call before it was handed over, copied off the matching
   *  `review.completed`. Present only at the Auto level, where the chassis tried to answer
   *  first — and `too_destructive` is why this is carried at all: the reviewer's own
   *  refusal became a park, so the one thing it can say about an irreversible act is said
   *  here, on the card where the operator is deciding. */
  risk?: "low" | "high" | "too_destructive";
  /** The review's account of its verdict, in its own words — the axes it scored and, when
   *  a standing grant supplied the authorization, that it did. */
  reviewReason?: string;
  /** True once submitting a decision for this approval 409'd — the run had
   *  already resumed elsewhere (a second tab, a retried request) by the time
   *  this decision landed. Non-interactive: a refetch reconciles the transcript
   *  with whatever the winning decision actually did. */
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

/** What the operator said to one question, as the transcript reads it back. The words of
 *  the question come with it because the backend pairs the two — from the parked call's
 *  own arguments, never from what the client sent — so a card cannot restate a question
 *  as something other than what was asked. */
export interface AnsweredQuestion {
  question: string;
  selections: string[];
  text?: string;
}

/** Whether a parked call has been answered — the one test, because `answers` is an
 *  *array* and an empty one is truthy. A malformed `ask_user` can parse to no questions
 *  at all, and treating its empty reply list as an answer both retires a park nobody
 *  answered and renders a card with a heading and nothing under it. */
export const isAnswered = (question: Question): boolean =>
  (question.answers?.length ?? 0) > 0;

/** A parked `ask_user` call: everything the agent asked, in one place, answered in one
 *  submit. The call is the unit because the run resumes on the call, not on a question. */
export interface Question {
  toolCallId: string;
  questions: QuestionSpec[];
  /** True once a submitted answer 409'd — the run had already resumed elsewhere.
   *  Same meaning, and the same reconciliation, as `Approval.stale`. */
  stale?: boolean;
  /** What was said, once it has been said — one entry per question, positionally.
   *  Present is the whole state: an answered call is no longer a park (the dock has
   *  nothing to collect) and starts being a row in the transcript instead. */
  answers?: AnsweredQuestion[];
}

/** Lifecycle of a host-machine command (`run_host_command`) — the one
 *  approval-gated tool that runs on the real host instead of the sandbox. */
export type HostCommandPhase =
  | "pending" // awaiting the operator's approval
  | "running" // approved; executing on the host
  | "ok" // finished, exit 0
  | "error" // finished non-zero, or the launch failed
  | "denied" // the operator refused it
  | "stale"; // a decision 409'd — already resolved elsewhere (see `Approval.stale`)

/** How far a command **declared** it needs to reach, on the call itself: the VM with no
 *  network, the VM with the allowed domains, or the operator's own machine — checked
 *  against every path the command names before anything runs. */
export type CommandReach = "offline" | "network" | "host";

/**
 * What a call that runs something declared, and what held it to that.
 *
 * Its own shape because **not every command renders as a terminal**. The foreground
 * shell does, and a backgrounded one does not — it is a handle the agent checks on
 * later, so it renders as an ordinary tool card — and both run a command line on the
 * operator's machine under a declaration and a fence. One set of facts, carried by both
 * kinds of row, rendered by one component.
 */
export interface CommandBoundaryFacts {
  /** How far the call **declared** it needs to go. */
  reach?: CommandReach;
  /** Whether an OS fence built from that declaration was actually applied. Not implied
   *  by `reach`: a host with no sandbox primitive has no fence to build. */
  fenced?: boolean;
  /** Why it ran unfenced — present only when `fenced` is false. */
  unfencedReason?: string;
  /** Whether a VM command ran with the network, which an `offline` declaration can
   *  differ from. Absent for a host command and on results from before it was sent. */
  network?: boolean;
  /** A permission failure in the output that the fence itself caused, said in words,
   *  so a denied write reads as the fence holding rather than as a broken command. */
  fenceNote?: string;
}

/** A host shell command rendered as a single persistent terminal: the exact
 *  command, the approval gate, and — once it runs — its captured output. Folded
 *  from the run's `tool.started`/`approval.required`/`tool.completed` events
 *  (warm) or the persisted tool call (cold), all keyed by `toolCallId`. */
export interface HostCommand extends CommandBoundaryFacts {
  toolCallId: string;
  /** The namespaced tool that asked. More than one renders as a terminal — the
   *  sandboxed host command and the worktree shell — and a conversation grant is
   *  recorded against a tool *name*, so the card cannot assume which one it holds. */
  name: string;
  /** The exact command line the agent asked to run on the host. */
  command: string;
  /** Plain-language description of the effect, shown for the approval decision. */
  explanation?: string;
  /** What a review made of this command before it was handed over, copied off the
   *  matching `review.completed` — the same two fields an `Approval` carries, for the
   *  same reason. A terminal is where the shell commands land, and the shell is where
   *  `too_destructive` actually shows up (`git commit --amend`, `rm -rf build`). */
  risk?: "low" | "high" | "too_destructive";
  reviewReason?: string;
  phase: HostCommandPhase;
  /** Captured output streams, present once the command has run. `exitCode` is null
   *  for a command that timed out — it never exited, which is a different fact from
   *  not having run yet (undefined). */
  exitCode?: number | null;
  stdout?: string;
  stderr?: string;
  timedOut?: boolean;
  /** A short failure hint, or a launch error. */
  error?: string;
  /** Wall clock around the spawn. Set from the result's own measurement rather than
   *  timed here — a client stopwatch would be measuring the stream, not the command. */
  elapsedMs?: number;
  /** Output streamed in while the command is still running, appended from the run's
   *  `tool.progress` deltas.
   *
   *  Deliberately **one string** where the settled command has two: mid-flight the wire
   *  carries stdout and stderr interleaved with no field to tell them apart, and
   *  guessing would put half a stack trace in the wrong stream. It is what the terminal
   *  shows while `phase` is `running`; the result is the record, and the card prefers
   *  `stdout`/`stderr` the moment the call completes. */
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

/** A **version** of the conversation's View (`view.snapshot`) — minted by a `show`.
 *  A git-style, point-in-time capture of the agent's sandbox tree (its code, browsed
 *  and diffed via `/views/snapshots/{id}/…`) plus how it previews. Conversation-scoped
 *  (the panel reads it); the inline chip references it by id. `summary` is a compact
 *  change tally, e.g. `"+2 ~1 -0"`. */
export interface ViewSnapshotRef {
  snapshotId: string;
  title?: string;
  createdAt: string;
  filesChanged: number;
  summary: string;
  /** The static preview the version was shown with, or null for a live/auto preview. */
  preview: ViewPreviewRef | null;
  /** Whether the operator has pinned this version as a "keeper" (backend-owned;
   *  `POST /views/snapshots/{id}/keeper`). Optional — absent until wired. */
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

/** One renderable unit of an assistant turn. A turn is an *ordered* list of
 *  these — the agent's true emission sequence (think → tool → text → think →
 *  tool → …), not regrouped into fixed lanes. Thinking and text arrive as deltas
 *  folded onto a trailing block of the same kind; a new block starts whenever the
 *  kind changes, so a turn naturally holds *multiple* thinking blocks interleaved
 *  with tools and text. `id` is stable for keyed rendering. */
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
/** Text the chassis put in front of the model that nobody in the thread wrote — the
 *  project's instruction files, the skill catalog, the plan reminder, the date
 *  (`context.injected`).
 *
 *  It sits on the same rail as the work because it happened in the same sequence, and it
 *  must read as categorically *not* work: the model did not do this, we did it to the
 *  model. `ContextInjectionCard` carries that distinction visually. */
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

/** One fold in flight: what triggered it, roughly what is going into it, the summary as
 *  it is written, and whether it has landed.
 *
 *  It rides on a **compaction turn** (`ChatMessage.compaction`), not on a block of the
 *  assistant's turn. It used to be a row on the assistant's rail, which was wrong in two
 *  ways at once: a fold the operator started by hand has no assistant turn to sit on, so
 *  that whole path rendered nothing at all; and a fold that *did* render put the same
 *  event on screen twice, once as a rail row and again as the divider it settled into.
 *  One event, one turn — which is also what makes the live fold and the settled fold the
 *  same shape, and lets the operator read what is happening rather than watch a count. */
export interface CompactionProgress {
  reason: CompactionReason;
  /** Messages about to be folded. */
  messages: number;
  /** Coarse char-based estimate of what they cost, in the same proxy the gauge uses. */
  tokensEstimate: number;
  /** The summary as the model writes it, accumulated from `compaction.delta`.
   *
   *  **The model's working, not the checkpoint** — unstripped, unanchored, unfenced. It
   *  exists so the pause has something in it, and it is discarded when the fold settles:
   *  what the operator keeps is the parsed summary on the compaction turn, which has been
   *  through all the handling this text has not. */
  summary?: string;
  done?: boolean;
}

/** An action the chassis ruled on in the operator's place, at the Auto permission level
 *  (`review.started` / `review.completed`).
 *
 *  It sits immediately ahead of the call it judged, and it is the whole of what makes Auto
 *  acceptable: a level whose proposition is "give my answers for me" is only worth having
 *  if the operator can read afterwards what was decided and on what grounds. Absent it,
 *  a tool call they never approved is indistinguishable from a gate that failed open. */
export interface ReviewBlock {
  kind: "review";
  id: string;
  review: Review;
}

/** One review: what the action would do at its worst, how it was ruled on, and — when a
 *  model rather than the deterministic judge ruled — the three axes it scored.
 *
 *  `decision` is undefined only while the review is in flight, which is a real state on a
 *  live turn: the row appears the moment the review starts, so a thread that pauses for a
 *  second on a model call reads as "being checked" and not as a stall. */
export interface Review {
  toolCallId: string;
  /** The namespaced tool the model asked for. */
  name: string;
  /** The action's worst case, in the same words the reviewer judged. */
  summary: string;
  /** The act's own content where the tool has some worth reading — a delegated task, the
   *  words a skill is being rewritten with, the reason given for opening a credential.
   *  Undefined for most tools. Shown because the reviewer ruled on it: a decision made in
   *  the operator's place should be reviewable on the same material. */
  detail?: string;
  /** How far a shell command said it needs to go. Undefined for acts that declare
   *  nothing at all, which is not the same as declaring the widest reach. */
  reach?: CommandReach;
  /** Whether the command will run with the network, which an `offline` declaration can
   *  differ from: an install or a named address gets it anyway. */
  network?: boolean;
  decision?: "allow" | "ask" | "block";
  /** Which stage settled it: the structural judge, or the model. */
  stage?: "judge" | "reviewer";
  /** On which ground the judge cleared it, when it did. */
  tier?: "read" | "sandbox" | "workspace";
  /** Whether an OS fence was available to hold the command to what it declared. False is
   *  why an ordinary contained command reached a reviewer, and it is the operator's to
   *  fix — so it is shown rather than inferred from the absence of a tier. */
  fenced?: boolean;
  reason?: string;
  risk?: "low" | "high" | "too_destructive";
  authorization?: "explicitly_no" | "neutral" | "explicitly_yes";
  correctness?: string;
}

/** A host-machine command, rendered as a persistent terminal. */
export interface HostCommandBlock {
  kind: "host_command";
  id: string;
  command: HostCommand;
}
/** A sensitive action paused for the operator's decision.
 *
 *  Folded into the turn like any other block, but **not rendered on the rail**: a parked
 *  run is answered in the dock that takes over the composer (`ParkDock`), where it cannot
 *  scroll out of reach and cannot sit next to an input that would do nothing. The block
 *  is where the dock reads its pending set from, and is filtered out of the transcript's
 *  own grouping (`blocks.ts`). What stays behind afterwards is the tool call itself. */
export interface ApprovalBlock {
  kind: "approval";
  id: string;
  approval: Approval;
}
/** A question paused for the operator's answer. Rendered in the dock, exactly like
 *  `ApprovalBlock` and for the same reasons. */
export interface QuestionBlock {
  kind: "question";
  id: string;
  question: Question;
}
/** An inline chip marking a version the agent `show`ed during the turn — rendered in
 *  the transcript as a compact chip that opens that version in the viewport. The
 *  version itself is conversation-scoped (a `ViewSnapshotRef` in the snapshots list);
 *  the chip only references it by id and labels it. */
export interface ViewVersionBlock {
  kind: "view_version";
  id: string;
  snapshotId: string;
  title?: string;
  /** The preview kind, for the chip icon (null/absent ⇒ a live/auto preview). */
  previewKind?: "html" | "image" | "text" | "other" | null;
}
/** The View's live head, started during the turn. Rendered as a compact LIVE chip
 *  that opens the viewport. */
export interface ViewLiveBlock {
  kind: "view_live";
  id: string;
  live: ViewLiveRef;
}
/** A web source the turn's `web_search`/`web_fetch` calls surfaced
 *  (`citation.added`), rendered as a compact Sources row beneath the answer. Its display
 *  number is its position in that deduped row, so no per-source index is carried. */
export interface Citation {
  /** Null for a corpus passage, which has a locator rather than an address — read
   *  `kind` before rendering a link. */
  url: string | null;
  title?: string;
  /** What the source *is*, and what repeat sightings fold by. The backend supplies it,
   *  because a web source and a corpus passage do not fold by the same field. Optional
   *  only for the cold path, which has not been given one yet. */
  key?: string;
  kind?: "web" | "corpus";
  /** How far the run got: `listed` (returned by a search, never opened) < `read` (its
   *  text went in front of the model) < `cited`. The backend may emit one source several
   *  times as it climbs; the highest rung wins. */
  engagement?: "listed" | "read" | "cited";
  snippet?: string;
  /** The source's own date, exactly as its provider reported it. Never parsed by the
   *  backend, so it is a bare string and may be any format. */
  published?: string;
  retrievedAt?: string;
  sourceId?: string;
  ref?: string;
}

/** The ladder's order, for folding several sightings of one source. Mirrors
 *  `core/citations.ENGAGEMENT_ORDER` — which of two claims is the stronger is a fact
 *  about the vocabulary the backend owns, not a rendering choice made here. */
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
  /** User turns: the operator's prompt text. Assistant turns: unused — the
   *  answer lives in the `text` blocks of `blocks` (kept "" so copy/edit/version
   *  paths that touch user content stay role-agnostic). */
  content: string;
  /** User turns: the non-text parts split out of the operator's bubble — attachment
   *  text, a routine's previous run — which `content` no longer holds. */
  parts?: ContentPart[];
  /** `notice` rows: the kind (`open-tasks`); `content` is the notice body. */
  notice?: string;
  /** Assistant turns: the ordered block sequence (the single source of truth for
   *  what the turn rendered). Absent on user turns. */
  blocks?: AssistantBlock[];
  /** Assistant turns: web sources the turn's `web_search`/`web_fetch` calls
   *  surfaced, in citation order. Absent on user turns and turns with no web
   *  tool use. */
  citations?: Citation[];
  /** True when the run backing this turn ended with `outcome: "blocked"` (a
   *  usage/loop/context/time bound, not a normal finish) — rendered as a
   *  persistent inline notice rather than only the transient `limit.notice`
   *  toast. `blockedDetail` is the human-readable reason. */
  blocked?: boolean;
  blockedDetail?: string;
  /** The run this assistant turn streams from — needed to approve/cancel it. */
  runId?: string;
  /** True while tokens are still streaming in. */
  streaming?: boolean;
  /** True from the moment this turn is submitted until its run's first event
   *  arrives — the backend only emits `run.started` once it clears the
   *  concurrency semaphore, so a turn can sit queued behind other runs for a
   *  while with nothing to show yet. Rendered as an explicit "queued" state
   *  instead of the streaming shimmer, so a wait behind the slot limit doesn't
   *  read as a stalled model. */
  queued?: boolean;
  /** True when the SSE transport exhausted its reconnect budget without a
   *  terminal event — the run may still be alive server-side, but this client
   *  lost its live connection to it. Distinct from `streaming`/settled: the
   *  turn is neither actively updating nor over, so it needs its own re-attach
   *  affordance rather than silently looking finished or frozen. */
  detached?: boolean;
  createdAt: string;
  /** Model/endpoint that produced an assistant message. */
  model?: string;
  /** 0-based position of this turn among its sibling versions (branches). */
  versionIndex?: number;
  /** Total sibling versions for this turn (≥1); >1 means it can be cycled. */
  versionCount?: number;
  /** Whether the operator has pinned this turn (backend-owned). */
  pinned?: boolean;
  /** User turns: ids of the uploads attached to this message. Rendered as
   *  read-only chips on the sent turn; absent/empty on assistant turns. */
  attachmentIds?: string[];
  /** User turns: workspace-relative paths the operator named with `@`. Rendered as
   *  read-only chips beside the attachments; absent/empty everywhere else. */
  fileRefs?: string[];
  /** Compaction dividers only: what the fold actually cost. `foldedMessages` counts
   *  **messages**, not exchanges — a plain exchange is two of them and a tool-heavy
   *  turn many more, so this is deliberately not called turns; the backend doesn't
   *  count turns at fold time and the divider must not imply it did. `tokensBefore`
   *  estimates the messages that were folded, `tokensAfter` the summary that replaced
   *  them — *not* the next turn's footprint, which also carries whatever tail the
   *  backend retained. Both are coarse char-based proxies, hence the `~` on screen.
   *
   *  The backend always sends all three (0 when it has nothing to report), so read
   *  them with a `> 0` guard, not a presence check. Optional here only so an older
   *  backend still renders the divider. */
  foldedMessages?: number;
  tokensBefore?: number;
  tokensAfter?: number;
  /** Compaction dividers only: what triggered the fold. A fold the operator asked for
   *  and a fold the provider forced are the same divider otherwise, and they are not
   *  the same event to have lived through. Carried both live (on the run's
   *  `conversation.compacted`) and cold (the backend stores it on the checkpoint), so a
   *  reload says what the operator watched happen. Still optional: a checkpoint folded
   *  before the backend recorded reasons has none, which is why the divider states it
   *  as an extra segment rather than building its label around it. */
  compactionReason?: CompactionReason;
  /** Compaction dividers only: the summary already split into its sections, parsed by
   *  the backend. `content` stays the stored text; this is what the divider renders.
   *
   *  Parsed there rather than here because the roster that decides where a section ends
   *  is a security boundary — a heading the summarizer quoted out of a fetched page must
   *  not be able to open one and carry what follows it out of the untrusted fence. The
   *  live event and the cold read call the same function on the same string, so the two
   *  cannot drift. Empty for a checkpoint whose text parses into nothing, and on an older
   *  backend: the divider falls back to `content` then. */
  summarySections?: SummarySection[];
  /** Compaction turns only, and only while one is **in flight**: what the fold is doing
   *  right now, and the summary as the model writes it.
   *
   *  A fold takes tens of seconds on a local endpoint, so it is a turn in the transcript
   *  from the moment it starts rather than one that appears once it is over — the same
   *  reason an assistant turn opens on its first delta. When `conversation.compacted`
   *  lands, this live turn is dropped and the settled one is seated at the fold boundary,
   *  which is a different place in the list: see `foldAnchor.ts`.
   *
   *  Absent on every settled compaction turn, warm and cold alike. */
  compaction?: CompactionProgress;
}

/** The in-flight run driving a conversation, when one exists. Present on a cold
 *  read taken mid-stream (e.g. a page reload) so the client can reattach to the
 *  live run and replay what it missed instead of rendering a reply-less thread. */
export interface ActiveRun {
  id: string;
  /** What the run is — a chat turn, or a fold, which has no assistant turn to seed. */
  kind: string;
  status: string;
  lastSeq: number;
  /** The persisted row this run is answering. The backend writes the operator's prompt
   *  as the turn begins, so the streaming answer belongs directly after this row; null
   *  for a run with no prompt (a fold) and briefly before a chat run has recorded one. */
  promptMessageId: string | null;
}

export interface ChatSession {
  id: string;
  title: string;
  /** No stored title yet, so `title` is the derived fallback. */
  untitled: boolean;
  messages: ChatMessage[];
  /** Context-window state reconstructed from the thread's last turn, or null
   *  when unavailable. Seeds the header meter on load. */
  context: ContextUsage | null;
  /** The thread's cumulative readout, rebuilt by the backend from the stored
   *  messages; null for a thread that has never run. Seeds the composer's readout
   *  line on load, so it reports the same totals it did live. */
  stats: ConversationStats | null;
  /** Set only while a turn is still streaming server-side; null otherwise. */
  activeRun: ActiveRun | null;
  /** Workspace snapshots captured across the thread (newest last), seeding the
   *  viewport's git-style history on load. */
  snapshots: ViewSnapshotRef[];
  /** How far the model may go in it. Seeded on load so the composer's control comes
   *  back where the operator left it instead of at the default. */
  permission: PermissionLevel;
}

/** The backend's status for the run driving a thread, when one is live. The
 *  server derives it from the run registry; the frontend only renders it —
 *  `awaiting_input` is a run parked on the operator's approval decision, the rest
 *  are plain in-flight work. */
export type ChatActivity = "queued" | "running" | "awaiting_input";

/** How the most recent **terminal** run for a thread ended — the other half of the
 *  story `activity` tells, and the half that used to collapse into nothing.
 *
 *  **Absent means "nothing known", never "nothing happened".** The backend derives it
 *  from an in-memory, bounded run registry, so a restart or enough traffic since leaves
 *  a thread that plainly finished with no outcome at all. Nothing may read the absence
 *  as idleness, an unfinished run, or a thread that never ran. */
export type ChatOutcome = "done" | "error" | "blocked" | "cancelled";

export interface ChatSummary {
  id: string;
  title: string;
  updatedAt: string;
  /** When the thread was opened, ISO-8601 — the session clock's input (§10.16).
   *  Distinct from `updatedAt`, which moves with every turn: the mission clock
   *  measures how long this thread has been open, not how recently it spoke. */
  createdAt: string;
  messageCount: number;
  /** Last-message snippet for preview cards. */
  preview?: string;
  /** What the agent did in this thread, in two or three sentences the backend's
   *  utility model wrote once the thread had been idle a while.
   *
   *  One value, replaced each time it is regenerated — there is no history of these.
   *  Absent until the backend's sweep has run, which is the ordinary state of a thread
   *  that is still being worked on, so every reader has to render without it. */
  workSummary?: string;
  /** The model the conversation last ran on (its most recent answer's model). */
  model?: string;
  /** Set while a run drives this thread; absent when it's idle. */
  activity?: ChatActivity;
  /** How this thread's last terminal run ended, when the backend still remembers.
   *  Read it only at rest: `activity` is the live truth and takes precedence, and the
   *  two can be set at once (a thread that finished and has already been sent to
   *  again). See `ChatOutcome` for why absence says nothing. */
  lastOutcome?: ChatOutcome;
  /** The **basename** of the directory of the project the thread is filed under. Absent
   *  for an unfiled thread, and for one whose project has since been deleted. Never a
   *  path: the backend resolves it and the full path stays there. */
  workspace?: string;
  /** The project the thread is scoped to — what the rail files it under, whether or not
   *  it has a worktree yet. The rail lists every directory the operator works in,
   *  including ones with no threads yet, so it groups by joining on this rather than on
   *  the name above; two directories can share a basename and filing by name would merge
   *  them. */
  projectId?: string;
  /** The custom agent the thread runs as. Absent for the default agent. */
  agentName?: string;
  /** The thread this one was opened from, when it belongs to an agent tree. */
  parentId?: string;
}

/** How far the model may go in a thread — the backend's `services/permissions`
 *  vocabulary, and the second axis of a conversation. It **moves**:
 *  it is persisted on the thread and changed by sending at a different level, so a
 *  reload comes back where the operator left it. */
export type PermissionLevel = "plan" | "manual" | "edit" | "auto" | "yolo";

export interface PermissionLevelSpec {
  id: PermissionLevel;
  /** Sentence case — the interface naming the thing to the operator. Says what the
   *  level *does* in plain words ("Ask first"), where the id is the backend's
   *  vocabulary: an operator choosing how much rope to give should not have to know
   *  that `manual` means the model asks and `edit` means it stays in its workspace. */
  label: string;
  /** What the model may do, said as the operator would say it. */
  description: string;
}

/** The five, ordered by how much rope they give — the same order the plan's table
 *  reads, so moving down the list is always moving towards *less*. */
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
  // The one level whose description has to be a warning rather than a summary. Every
  // other row says what the model may do; this row's useful content is what *stops* —
  // nothing — and the two things that survive, which are the sandbox around a command
  // and the transcript afterwards. Written in the second person and in plain words
  // because it is the last thing an operator reads before choosing it.
  {
    id: "yolo",
    label: "No limits",
    description:
      "Free rein. Nothing stops for you — not commands, mail, credentials or anything it can't undo.",
  },
];

/** The level a free-typed word names — its id **or** its label, in any case — or null.
 *
 *  For the one path where it is not already narrowed: `/level auto` in the composer,
 *  where the operator types the word themselves. Derived from the list above rather than
 *  re-listing them, so a further level is offered here the moment it is declared.
 *
 *  `/level ask first` has to work as well as `/level manual`: the control shows the
 *  label, so the label is the word the operator has in front of them, and refusing it
 *  in favour of an id they have never been shown would make the command a lookup
 *  exercise. Presentation only — what comes out is an id, and the backend re-validates
 *  it like any other. */
export function parsePermissionLevel(value: string): PermissionLevel | null {
  const word = value.trim().toLowerCase();
  return (
    PERMISSION_LEVELS.find(
      (spec) => spec.id === word || spec.label.toLowerCase() === word,
    )?.id ?? null
  );
}

/** The level after `level` in the list's order, for the composer's Shift+Tab cycle.
 *
 *  **It never lands on `yolo`.** That is the one level that asks nothing at all, and it
 *  is reachable only by naming it — a keystroke pressed a few times too often must not
 *  be how a thread ends up there. From `yolo` itself the cycle wraps to the strictest
 *  level rather than stepping back down one: leaving the widest level by a reflex should
 *  land where a reflex is safe. */
export function nextPermissionLevel(level: PermissionLevel): PermissionLevel {
  const cycle = PERMISSION_LEVELS.filter((spec) => spec.id !== "yolo");
  const at = cycle.findIndex((spec) => spec.id === level);
  // `-1` is `yolo` (or anything the list no longer holds), and `(-1 + 1) % n` is the
  // head of the list — the wrap described above falls out of the arithmetic.
  return cycle[(at + 1) % cycle.length]!.id;
}

/** The level a thread runs at when nothing says otherwise — the backend's
 *  `DEFAULT_PERMISSION`, which is also each mode's default today.
 *
 *  Auto reaches no further than Edit; it differs in who answers at the boundary, and
 *  what it settles on its own is contained offline work an OS sandbox holds to the
 *  worktree. This is the default for a **new** thread only — an existing one arrives
 *  with its own stored level and keeps it. */
export const DEFAULT_PERMISSION_LEVEL: PermissionLevel = "auto";

/** The level that does the least — the backend's `STRICTEST_PERMISSION`, and where every
 *  reading that is *not yet known* lands. A level rides every send and the backend
 *  persists what it is sent, so an unknown one has to resolve somewhere that cannot widen
 *  a thread the operator already narrowed. Distinct from the default on purpose: the
 *  default is what a thread with no level yet *starts* at, which is a different question
 *  from what to do about one whose level exists and has not arrived. */
export const STRICTEST_PERMISSION_LEVEL: PermissionLevel = "plan";

/** Whatever the wire said, as a level this build has a rule for.
 *
 *  The two failure directions are different on purpose. **Nothing** means the thread
 *  has no level yet — a fresh conversation, or a backend that predates the column —
 *  and gets the default. A value that is **present and unreadable** is a thread
 *  claiming something this build cannot honour, so it degrades to the level that does
 *  the least, which is the direction the backend degrades in too. Treating the two
 *  the same would either put every new thread in read-only or let an unknown string
 *  widen what the model may do. */
export function permissionLevel(value: string | undefined): PermissionLevel {
  if (!value) return DEFAULT_PERMISSION_LEVEL;
  return PERMISSION_LEVELS.some((spec) => spec.id === value)
    ? (value as PermissionLevel)
    : STRICTEST_PERMISSION_LEVEL;
}

/** One decision in an approval response (mirrors the backend's shape). */
/** What the operator said to one question. `selections` carries the labels they chose,
 *  exactly as offered; `text` is what they wrote instead of — or as well as — choosing.
 *  The server checks both against the parked call and renders the prose itself, so this
 *  never carries the question's own words back. */
export interface QuestionReply {
  selections: string[];
  text?: string;
}

/** One parked `ask_user` call, answered. `replies` is positional: one per question in
 *  the call, in the order they were asked. */
export interface QuestionAnswer {
  tool_call_id: string;
  replies: QuestionReply[];
}

export interface ApprovalDecision {
  tool_call_id: string;
  approved: boolean;
  message?: string;
  override_args?: Record<string, unknown>;
  /** How long this yes lasts. "once" (default) is this call only. "conversation" also
   *  records an auto-approval grant so the same *act* isn't re-prompted for the rest of
   *  the thread — on a tool that runs a command that is the command, derived server-side
   *  from the parked call. "conversation_tool" is the wider pick: the whole tool, offered
   *  in its own words because it is a materially larger thing to say. Only acted on by the
   *  backend when `approved` is true. */
  scope?: "once" | "conversation" | "conversation_tool";
  /** Which kind of no. Read only when `approved` is false, and it changes nothing about
   *  what runs — both stop the call — only what the model is told happened: "deny" is a
   *  refusal, "revise" (default "deny") is a request for a different version of the same
   *  act. Generic rather than plan-specific, though a submitted plan is where it earns
   *  its keep. */
  intent?: "deny" | "revise";
}

/** The written plan a plan-mode turn submitted — the document, not the checklist.
 *
 *  `revision` counts up on every resubmission, which is how the panel tells a revised
 *  plan (a fresh arrival, worth opening for) from the same plan re-rendering. */
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

/** A live conversation-scoped auto-approval grant — the operator's visible + revocable
 *  record of what auto-approves for the rest of the thread. The TTL is backend-owned and
 *  not surfaced here (the strip shows the act, and nothing else). */
export interface ApprovalGrant {
  /** The namespaced tool name that auto-approves, e.g. "corpus_retrieve". */
  toolName: string;
  /** The command it is scoped to, as the words it was read as (`["uv", "run", "pytest"]`)
   *  — empty for the whole tool. A tool that runs a command is granted per act, so one
   *  thread can hold several grants on the same tool; this is what tells them apart, in
   *  the chip and in the revoke. Kept as words rather than a sentence because the revoke
   *  sends it straight back: joining is for the chip's label. */
  commandPrefix: string[];
  /** Whether this is the operator's wider pick — their answer for the whole tool, which
   *  the reviewing level takes as the authorization rather than merely weighing. The chip
   *  needs it because an empty `commandPrefix` alone cannot say which of the two this is:
   *  a non-command tool's ordinary grant and a scheduled task's seed take the same shape. */
  decisive: boolean;
  /** Whether this tool's grants are scoped to a command at all. A tool that runs none has
   *  only one width, so "any command" is not a contrast there — it names commands that do
   *  not exist. */
  commandScoped: boolean;
}

/** A conversation's compaction state — the same shape for both reductions. `override` is
 *  the stored per-chat choice (`null` = inherit the operator's global setting); `effective`
 *  is the resolved on/off the UI renders. The backend owns the resolution — the frontend
 *  only reflects it. */
export interface CompactionState {
  override: boolean | null;
  effective: boolean;
}
