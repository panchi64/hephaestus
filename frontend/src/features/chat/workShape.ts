/** What a folded run of work amounts to — the last thing it did, and how much of
 *  it there is.
 *
 *  Two summaries have been tried here and both failed for the same reason: they
 *  reported *magnitude* where the operator was reading for *identity*. `7 Steps`
 *  ranked every turn in a thread identically. `Read ×4 · Web search · Write`
 *  named the tools, which was better, but a per-tool tally is still a number
 *  without a referent — whether the agent read four files or seven changes
 *  nothing about whether this fold is worth opening, and the `+N` overflow was
 *  worse still, counting kinds the operator could not see.
 *
 *  What actually distinguishes one fold from another is **what it last did**:
 *  `Read agent.py`, `Web search "pydantic ai streaming"`. That is a fact with a
 *  noun in it. It also does the work the old summary could not do at all —
 *  while the turn streams, settled work keeps folding in, so this line advances
 *  as the run progresses and is the fold's own proof of life.
 *
 *  The size of the run rides along as dim metadata rather than as the headline,
 *  because "is opening this worth it" is the one question a magnitude genuinely
 *  answers.
 *
 *  Pure data → data, like `blocks.ts`: no Solid, no DOM, so the rules stay
 *  testable and live in one place. */

import type { IconName } from "~/ui";
import { INJECTION_ICON, segmentLabel } from "./contextLabels";
import { toolEntry } from "./toolPresentation";
import type { AssistantBlock } from "./model";
import type { BlockGroup } from "./blocks";

/** The glyph for reasoning. Not a tool, so it has no registry entry to borrow —
 *  `cpu` is the machine doing its own work, and it is unclaimed by any tool
 *  category, so it can't be mistaken for one. Declared here rather than in the
 *  two components that draw it, so the settled reasoning row and the work log
 *  header cannot drift onto different glyphs for the same idea. */
export const THINK_ICON: IconName = "cpu";

/** One piece of work, in the same anatomy its own card uses: `glyph · Label ·
 *  what it was about`. The header speaking the sentence its rows speak is the
 *  point — the fold then reads as its own most recent row rather than as a
 *  different species of line sitting on top of them. */
export interface WorkStep {
  icon: IconName;
  /** The kind, in the interface's voice: "Read", "Web search", "Reasoning". */
  label: string;
  /** What it was about — a path, a query, a command. Absent when the call
   *  carried nothing salient, and always absent for reasoning, which is about
   *  nothing nameable. */
  detail?: string;
}

export interface WorkShape {
  /** The run's most recent step. Undefined only for a run with nothing in it,
   *  which never reaches the header. */
  latest?: WorkStep;
  /** The block `latest` was read off. The indented row keys its reveal on this, so
   *  a new step replays the arrival and a re-render of the same one does not. */
  latestId?: string;
  /** How many rows expanding would reveal — one per group, which is exactly what
   *  the open log renders. */
  steps: number;
}

/** Host commands borrow the registry entry of the tool that produces them, so
 *  the header's glyph matches the terminal card's. */
const HOST_TOOL = "shell_run_on_host";

/** The one glyph a review row leads with, wherever it appears — the header's summary and
 *  the row itself must agree, or the fold and its contents read as two different events. */
const REVIEW_ICON: IconName = "review";

/** One block as a step, or `undefined` for a block kind that is not work the log
 *  reports (nothing else reaches a fold today, but the log's membership rules
 *  live in `blocks.ts` and this must not assume they never widen). */
function stepOf(block: AssistantBlock): WorkStep | undefined {
  switch (block.kind) {
    case "thinking":
      return { icon: THINK_ICON, label: "Reasoning" };
    case "tool": {
      const { icon, label } = toolEntry(block.tool.name);
      // The salient argument — never the narration, which the header already leads
      // with (`reasonOf`), so the row under it would only say it twice. Never
      // `tool.args` either: the card falls back to the full `k=v` dump because an
      // open card has room for it, and this row does not — a serialized argument
      // blob here would push the one readable word off the end of the row.
      return { icon, label, detail: block.tool.detail };
    }
    case "host_command": {
      const { icon, label } = toolEntry(HOST_TOOL);
      return { icon, label, detail: block.command.command };
    }
    case "context":
      return {
        icon: INJECTION_ICON,
        label: segmentLabel(block.injection.contributor),
        detail: "injected",
      };
    case "review":
      // Named by the tool it judged, not by the review — a collapsed log ending on
      // "Review · Shell · refused" says what happened; one ending on "Review"
      // alone says only that something did.
      return {
        icon: REVIEW_ICON,
        label: toolEntry(block.review.name).label,
        detail: block.review.decision
          ? `review: ${block.review.decision}`
          : "reviewing",
      };
    // No View arm: a chip is a result and never folds (`isCollapsible`), so a
    // case for one here would be a summary that can't be reached.
    default:
      return undefined;
  }
}

/** Why a block was run, in the model's words, or `undefined` for a kind that has
 *  no reason to give. A host command's `explanation` already folds in the shell's
 *  narration (`commandReason`), so both terminals speak here. */
function reasonOf(block: AssistantBlock): string | undefined {
  switch (block.kind) {
    case "tool":
      return block.tool.narration;
    case "host_command":
      return block.command.explanation;
    default:
      return undefined;
  }
}

/** The last block in a run that `pick` answers for, scanned from the end — a run
 *  can be forty blocks long and both questions asked of it are about the *last*
 *  of something. */
function lastOf<T>(
  groups: BlockGroup[],
  pick: (block: AssistantBlock) => T | undefined,
): { value: T; id: string } | undefined {
  for (let g = groups.length - 1; g >= 0; g--) {
    const blocks = groups[g].blocks;
    for (let b = blocks.length - 1; b >= 0; b--) {
      const value = pick(blocks[b]);
      if (value !== undefined) return { value, id: blocks[b].id };
    }
  }
  return undefined;
}

/** The run's most recent *reason* — the model's own sentence for why it made a
 *  call, which the collapsed header leads with.
 *
 *  Its own scan rather than a field on `WorkShape`, because it is asked of a
 *  different set: the reason is read over the whole run (a pinned live call's
 *  narration is the freshest intent), the shape over the fold only. It also scans
 *  past steps that carry no reason at all (reasoning, injected context, a
 *  review): pinning it to the last step would blank the header every time the
 *  model paused to think, and the intent it last stated is still the one it is
 *  acting on. */
export function latestReason(groups: BlockGroup[]): string | undefined {
  return lastOf(groups, reasonOf)?.value;
}

/** The shape of a run of work groups — the latest step and the fold's size.
 *
 *  There is no failure count on purpose: ordinary failures are routine, and a count
 *  on the shut log would surface exactly what the fold keeps quiet. */
export function workShape(groups: BlockGroup[]): WorkShape {
  const last = lastOf(groups, stepOf);
  return last
    ? { latest: last.value, latestId: last.id, steps: groups.length }
    : { steps: groups.length };
}
