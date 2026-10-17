/** Pure shaping of an assistant turn's ordered `blocks` for rendering and copy.
 *  No Solid/DOM here — just data → data, so the rules (grouping, compaction,
 *  transcript assembly) stay testable and live in one place. */

import { isAnswered, isLiveStatus } from "./model";
import type {
  AssistantBlock,
  BlockKind,
  TextBlock,
  ToolInvocation,
} from "./model";

/** A run of consecutive collapsible work folds into a WORK LOG accordion once it
 *  reaches this many groups.
 *
 *  It is **one**: everything `isCollapsible` admits is settled process that has
 *  already had its chance to pin itself inline — a call in flight, a failure, a
 *  picture and a refusal all stay out of a fold by their own rule, not by being
 *  in too short a run. A floor of three left the remainder of every run on the
 *  rail, which read as work leaking out of the log rather than as work kept
 *  deliberately visible.
 *
 *  Kept as a constant rather than inlined: this is the knob the rule has been
 *  retuned on twice, and the shape of `flush` is what makes retuning it a
 *  one-line change. */
export const WORK_LOG_MIN_RUN = 1;

/** The answer the operator reads — every `text` block in order.
 *
 *  Blank passages are dropped rather than joined: a model routinely emits a bare
 *  newline between tool batches, and pasting an answer should not carry the gaps
 *  between the parts of it that were never written. */
export function answerText(blocks: AssistantBlock[] | undefined): string {
  return (blocks ?? [])
    .filter((b): b is TextBlock => b.kind === "text")
    .map((b) => b.text)
    .filter((text) => text.trim())
    .join("\n\n");
}

/** Every reasoning passage in order (for COPY REASONING). */
export function reasoningText(blocks: AssistantBlock[] | undefined): string {
  return (blocks ?? [])
    .filter((b) => b.kind === "thinking")
    .map((b) => b.text)
    .join("\n\n");
}

export function hasReasoning(blocks: AssistantBlock[] | undefined): boolean {
  return (blocks ?? []).some((b) => b.kind === "thinking");
}

/** The kinds that are *process* — the turn's account of what it did on the way to an
 *  answer, as opposed to the answer itself or a control the operator acts on.
 *
 *  This is what the work log is a log *of*, and drawing the line by kind rather than by
 *  whether a particular row happens to be foldable is what keeps one turn to one log.
 *  A failed call and a running one are still work; they are work that has to stay
 *  visible, which is a different question (`pinsRunInline`). */
const WORK: ReadonlySet<BlockKind> = new Set([
  "thinking",
  "tool",
  "context",
  "review",
  "host_command",
]);

/** Whether a turn has any collapsible layer worth an expand-all control. */
export function hasLayers(blocks: AssistantBlock[] | undefined): boolean {
  return (blocks ?? []).some((b) => WORK.has(b.kind));
}

/** What a call came back with, for the plain-text export. */
function callOutcome(t: ToolInvocation): string {
  return t.error ? `error: ${t.error}` : (t.result ?? "");
}

/** Flatten a turn to one plain-text block for COPY MESSAGE — reasoning, each
 *  tool/host call as `name(args) -> outcome`, decisions, then the answer, all in
 *  the order they happened. */
export function assembleTranscript(
  blocks: AssistantBlock[] | undefined,
): string {
  const parts: string[] = [];
  for (const b of blocks ?? []) {
    switch (b.kind) {
      case "thinking":
        parts.push(`REASONING\n${b.text}`);
        break;
      case "text":
        parts.push(b.text);
        break;
      case "tool": {
        const t = b.tool;
        // The narration leads, and it is carried separately because `args` no longer
        // holds it: `formatArgs` strips it so the open card does not print the row's
        // own headline twice. Without this line the export would be the one place the
        // agent's reason for a call is missing — and "why did it do that" is most of
        // what a pasted turn is pasted to answer.
        const why = t.narration ? `# ${t.narration}\n` : "";
        // A script is kept out of `args` for the card's sake, and its calls live on
        // its card rather than the rail — both would otherwise go missing here.
        const script = t.script ? `\n${t.script}` : "";
        const calls = (t.children ?? [])
          .map((c) => `\n  ${c.name}(${c.args}) -> ${callOutcome(c)}`)
          .join("");
        parts.push(
          `${why}${t.name}(${t.args})${script} -> ${callOutcome(t)}${calls}`,
        );
        break;
      }
      case "context":
        // Named as an injection rather than transcribed as a message, so a pasted
        // transcript can't read as something the operator or the model said.
        parts.push(
          `CONTEXT INJECTED (${b.injection.contributor})\n${b.injection.text}`,
        );
        break;
      case "review": {
        // The verdict and its grounds both, because a pasted transcript is often exactly
        // what an operator sends when asking why something ran — and "REVIEWED: allow"
        // without the reason answers none of that.
        const r = b.review;
        const verdict = r.decision ?? "in progress";
        parts.push(
          `REVIEWED ${r.name}: ${verdict}${r.reason ? ` — ${r.reason}` : ""}\n${r.summary}`,
        );
        break;
      }
      case "host_command": {
        const c = b.command;
        const out = c.error ?? c.stdout ?? "";
        parts.push(`$ ${c.command}${out ? `\n${out}` : ""}`);
        break;
      }
      case "approval":
        parts.push(
          `APPROVAL REQUIRED: ${b.approval.name} — ${b.approval.summary}`,
        );
        break;
      case "question":
        // Both halves once there are two. The answers are on the block itself now, so
        // the export no longer has to send a reader to the tool call above to find out
        // what was said — and a question still waiting exports as the question alone.
        parts.push(
          (
            b.question.answers ??
            b.question.questions.map((q) => ({
              question: q.question,
              selections: [] as string[],
              text: undefined as string | undefined,
            }))
          )
            .map((a) => {
              const said = [a.selections.join(", "), a.text]
                .filter(Boolean)
                .join(" — ");
              return said
                ? `ASKED: ${a.question}\nANSWERED: ${said}`
                : `ASKED: ${a.question}`;
            })
            .join("\n"),
        );
        break;
      case "view_version":
        parts.push(`[view version: ${b.title ?? "version"}]`);
        break;
      case "view_live":
        parts.push(`[live view: ${b.live.url}]`);
        break;
    }
  }
  return parts.join("\n\n");
}

/* ── Grouping ─────────────────────────────────────────────────────────────────
   Host commands batch into one card (the parked run resumes only on a decision
   covering every pending call), so consecutive blocks of that kind merge into a
   single group. Every other block stands alone.

   This assumes a park's pending calls arrive contiguously in the stream (true
   for a single park — the events for one step's gated calls are emitted back to
   back). If a future backend ever interleaves a non-gated block *between* two
   simultaneously-pending commands, they'd split across cards and each would
   submit a partial decision — unify the cards then. */

export interface BlockGroup {
  id: string;
  kind: BlockKind;
  blocks: AssistantBlock[];
}

const AGGREGATED: ReadonlySet<BlockKind> = new Set(["host_command"]);

/** Blocks the transcript does not render *while they are waiting*: the two the operator
 *  answers in the dock that takes over the composer (`ParkDock`).
 *
 *  Dropped here rather than never folded, because the block is still how the dock
 *  learns what is pending — and how it learns again after a reconnect replays the
 *  stream. Rendering them in both places would put the same decision on screen twice,
 *  with two submit buttons for one run that resumes once.
 *
 *  **Docking is a phase, not a kind, and a question leaves it.** Once answered it is no
 *  longer anything the dock collects, and it becomes the one thing the transcript most
 *  owes the operator: what they were asked and what they said. That used to be reachable
 *  only as the `ask_user` call's own result — prose, inside a collapsed work log, behind
 *  a disclosure — which is a record rather than a reading of it. An approval has no such
 *  second life: its outcome is the call that ran, and the review row beside it already
 *  says on what grounds. */
const DOCKED: ReadonlySet<BlockKind> = new Set(["approval", "question"]);

/** Whether this block is still waiting in the dock, as opposed to merely being of a
 *  kind that docks. */
const isDocked = (b: AssistantBlock): boolean =>
  DOCKED.has(b.kind) && !(b.kind === "question" && isAnswered(b.question));

export function groupBlocks(
  blocks: AssistantBlock[] | undefined,
): BlockGroup[] {
  const groups: BlockGroup[] = [];
  for (const b of blocks ?? []) {
    if (isDocked(b)) continue;
    const last = groups[groups.length - 1];
    if (last && last.kind === b.kind && AGGREGATED.has(b.kind)) {
      last.blocks.push(b);
    } else {
      groups.push({ id: b.id, kind: b.kind, blocks: [b] });
    }
  }
  return groups;
}

/** A host-command group the operator still needs eyes on — awaiting a decision
 *  (pending) or actively running on the host (live output). See `pinsRunInline`
 *  for the rest of what must not fold; this one is specifically about *live*. */
function hasLiveHost(group: BlockGroup): boolean {
  return group.blocks.some(
    (b) =>
      b.kind === "host_command" &&
      (b.command.phase === "pending" || b.command.phase === "running"),
  );
}

/** A tool call still running. Each tool block is its own group, so a parallel batch is
 *  a *run* of them — and a run is exactly what folds away, which would hide the
 *  spinners of every call still in flight. Same rule as `hasLiveHost`. */
function hasLiveTool(group: BlockGroup): boolean {
  return group.blocks.some(
    (b) => b.kind === "tool" && isLiveStatus(b.tool.status),
  );
}

/** A call that failed — the tool returned an error, or a host command came back
 *  non-zero. A denied host command is not this: that is a decision the operator
 *  already made, and it may fold away like any other settled terminal. */
function hasFailure(group: BlockGroup): boolean {
  return group.blocks.some(
    (b) =>
      (b.kind === "tool" && b.tool.status === "error") ||
      (b.kind === "host_command" && b.command.phase === "error"),
  );
}

/** A call that came back with a picture — a browser screenshot. */
function hasImages(group: BlockGroup): boolean {
  return group.blocks.some((b) => b.kind === "tool" && b.tool.images?.length);
}

/** A review that refused the call it judged. Distinct from a review that *parked* one:
 *  a park raises the dock, which takes over the composer and carries the question;
 *  a refusal is followed by nothing at all, so this row is the only account of it. */
function hasRefusal(group: BlockGroup): boolean {
  return group.blocks.some(
    (b) => b.kind === "review" && b.review.decision === "block",
  );
}

/** Work that must stay on screen whatever else folds: still in flight, waiting on
 *  a decision, failed, or carrying something to look at.
 *
 *  Failure is here and deliberately NOT in `hasLiveTool`, because the two mean
 *  different things and only one of them lights the rail. `liveToolGroupIds`
 *  drives the trunk's `info` mark, whose whole claim is "this is running *now*" —
 *  marking it for a call that failed a minute ago would be a lie in the one place
 *  the interface speaks in a stroke rather than words.
 *
 *  Without this, a failure was the single most hidden thing in a turn: the card
 *  auto-expands on error, but the work log folded shut around it, so the one
 *  event that should interrupt was the one event buried.
 *
 *  Images are here for the same reason and it bites hardest in the case that
 *  produced them: an agent that screenshots repeatedly makes a run of settled
 *  calls, which is exactly what folds — so without this the pictures would be
 *  hidden precisely when there are the most of them to see. A fold exists to hide
 *  undifferentiated process, and a picture of the page is not that. It costs the
 *  turn a strip of height per screenshot, knowingly. */
function pinsRunInline(group: BlockGroup): boolean {
  return (
    hasLiveTool(group) ||
    hasLiveHost(group) ||
    hasFailure(group) ||
    hasImages(group)
  );
}

/** Collapsible = process the operator doesn't have to read or act on inline:
 *  reasoning, tool calls that finished cleanly, and host terminals that are
 *  settled and not failed.
 *
 *  Three things break a work log run: answer `text` (the model writing *to the
 *  operator* — the one thing that should segment the log), a View chip, and
 *  anything `pinsRunInline` claims — a call in flight, a host command awaiting a
 *  decision, or a failure. The operator is watching it happen, or needs to know it
 *  went wrong. Everything else folds into one continuously growing log.
 *
 *  **The View chips used to fold and no longer do.** They are the only kind here
 *  that renders full-width rather than on the rail, because they are a result and
 *  not process — and a chip is the transcript's one handle into the viewport.
 *  That was survivable while a run had to reach three groups to fold; at a floor
 *  of one, a lone chip would become `Work log · View · 1 step` and the handle
 *  would be behind a disclosure.
 *
 *  An *unanswered* park is absent from this reckoning because it is absent from the
 *  transcript — it is answered in the dock, and `groupBlocks` emits no group for it. An
 *  **answered question does reach here, and must not fold**: it falls through to the
 *  `false` below for the same reason the View chip does. It is a result rather than
 *  process, it renders full-width rather than on the rail, and an exchange the operator
 *  has to expand a work log to find is one they will not read. */
function isCollapsible(group: BlockGroup): boolean {
  if (group.kind === "thinking") return true;
  // Injected context is the frame around the work, never the work — it has no state to
  // watch, nothing to act on, and it arrives in a clump at the head of every turn. If
  // anything in a turn should fold, it is this.
  if (group.kind === "context") return true;
  // A review that cleared or parked a call folds with the work it judged: the operator
  // can open the log and read it, and nothing about it needs them *now*. A review that
  // *refused* one does not — a call the chassis blocked outright on the operator's
  // behalf is the single thing in a turn they are most likely to disagree with, and
  // burying it inside a fold would make Auto's promise unverifiable in practice.
  if (group.kind === "review") return !hasRefusal(group);
  if (group.kind === "tool" || group.kind === "host_command")
    return !pinsRunInline(group);
  return false;
}

/** A text group with nothing in it — a whitespace-only passage, which a model
 *  routinely emits between two batches of tool calls.
 *
 *  It renders nothing, so it must not *segment* anything either. Left as an
 *  ordinary run-breaker it was the single largest source of work leaking out of a
 *  fold: one work log became two with no visible cause between them, and the
 *  remainder of the second run showed up as loose rows on the rail. Text is the
 *  one thing that legitimately breaks a log, and a blank passage is not text. */
function isBlankText(group: BlockGroup): boolean {
  return group.kind === "text" && !(group.blocks[0] as TextBlock).text.trim();
}

/** Every group with a call in flight. The trailing group is live by *position*; these
 *  are live by *state*, and with parallel calls the two are no longer the same set. */
export function liveToolGroupIds(groups: BlockGroup[]): Set<string> {
  return new Set(groups.filter(hasLiveTool).map((g) => g.id));
}

/** Every tool call in flight across a turn, in order — read across the whole turn
 *  because a parallel batch has no single trailing member that speaks for the rest. */
export function runningTools(
  blocks: AssistantBlock[] | undefined,
): ToolInvocation[] {
  return (blocks ?? []).flatMap((b) =>
    b.kind === "tool" && isLiveStatus(b.tool.status) ? [b.tool] : [],
  );
}

/* ── Compaction layout ────────────────────────────────────────────────────────
   Every maximal run of consecutive *work* becomes one WORK LOG accordion, and
   everything else — the answer, View chips, outputs — stays visible between the
   logs, in order, so the turn's true think → tool → text → … narrative survives.

   **What breaks a log is the answer, not a state.** Membership is decided by kind
   (`WORK`) and visibility separately (`pinsRunInline` via `isCollapsible`), which
   are two questions that used to be one. Deciding them together meant a failure, a
   live call, a screenshot or a refusal *ended* the log it belonged to and started
   another with the same name under it, so an ordinary turn with one failed call
   rendered as three items the operator had to read as one sequence. Now such a row
   sits inside the log, pinned open; the log is still forbidden from hiding it, and
   the turn still has one account of itself.

   A blank passage is neither answer nor work, and is skipped outright rather than
   counted as an answer — see `isBlankText`. */

/** A row inside a work log, and whether the fold is allowed to hide it. */
export interface WorkLogEntry {
  group: BlockGroup;
  /** Shown whether the log is open or shut — a call in flight, a failure, a picture,
   *  a refusal, or the live tail. See `pinsRunInline`. */
  pinned: boolean;
}

export type LayoutItem =
  | { type: "group"; group: BlockGroup }
  | { type: "worklog"; entries: WorkLogEntry[] };

/**
 * A stable identity for a layout item, across every recompute of the plan.
 *
 * `planTurnLayout` mints fresh objects each call, so a reference-keyed `<For>`
 * treats the whole turn as new every time the plan is rebuilt — on each new
 * block, and again when `streaming` flips at the end of a run. That tears down
 * and re-renders every row in the turn, which is a visible redraw at exactly the
 * moment the operator starts reading. Keying on this instead means a row is
 * created once and only genuinely new or regrouped rows move.
 *
 * The first block's id anchors both kinds: a group keeps its id as it grows, and
 * a work-log run is named by where it starts, so a run absorbing another group
 * stays the same item rather than becoming a different one.
 */
export function layoutItemKey(item: LayoutItem): string {
  return item.type === "worklog"
    ? `w:${item.entries[0]?.group.id ?? ""}`
    : `g:${item.group.id}`;
}

export function planTurnLayout(
  groups: BlockGroup[],
  opts: { streaming?: boolean } = {},
): LayoutItem[] {
  // While streaming, the trailing group is "live" — keep it inline, never folded.
  const activeIndex = opts.streaming ? groups.length - 1 : -1;
  const items: LayoutItem[] = [];
  let run: WorkLogEntry[] = [];
  const flush = (): void => {
    if (run.length >= WORK_LOG_MIN_RUN) {
      items.push({ type: "worklog", entries: run });
    } else {
      for (const entry of run)
        items.push({ type: "group", group: entry.group });
    }
    run = [];
  };
  groups.forEach((group, i) => {
    const active = i === activeIndex;
    // Transparent, not collapsible: a blank passage joins no run and emits no row,
    // so the work either side of it stays one run. The live tail is the exception —
    // a streaming text block is blank for its first delta and carries the caret,
    // and dropping it would blink the caret out at the start of every answer.
    if (!active && isBlankText(group)) return;
    if (!WORK.has(group.kind)) {
      flush();
      items.push({ type: "group", group });
      return;
    }
    // Every work group joins the log, including the ones the log may not hide.
    //
    // This used to flush the run at anything non-collapsible, which meant a single
    // failed call mid-turn produced *three* items — a log, the failure, then a second
    // log with the same name — and an operator reading down the turn had to stitch one
    // sequence back together out of them. The fold's promise is only that a live call,
    // a failure, a picture and a refusal stay visible; it was never that they should
    // cut the account of the turn in half. So they stay visible **in place**, and the
    // turn keeps one log.
    run.push({ group, pinned: active || !isCollapsible(group) });
  });
  flush();
  return items;
}

/* The collapsed work log's own summary lives in `workShape.ts` — what the run
   was made of, by tool, rather than the latest call or a bare step count. */
