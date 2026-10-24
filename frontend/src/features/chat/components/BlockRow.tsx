import { Match, Switch, type JSX } from "solid-js";
import type {
  ApprovalDecision,
  ContextBlock,
  HostCommandBlock,
  QuestionBlock,
  ReviewBlock,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ViewLiveBlock,
  ViewVersionBlock,
} from "../model";
import type { BlockGroup } from "../blocks";
import {
  LIVE_KEY,
  snapshotKey,
  versionIcon,
  type ViewItem,
} from "../viewport/viewItems";
import { AnswerText } from "./AnswerText";
import { Branch, type BranchEdge } from "./Branch";
import { ContextInjectionCard } from "./ContextInjectionCard";
import { HostCommandCard } from "./HostCommandCard";
import { QuestionAnswerCard } from "./QuestionAnswerCard";
import { ReasoningBlock } from "./ReasoningBlock";
import { ReviewCard } from "./ReviewCard";
import { ToolCallCard } from "./ToolCallCard";
import {
  classifyViewItem,
  viewItemTimeLabel,
  viewItemVersionLabel,
  ViewChip,
} from "./ViewChip";

type Resolve = (decisions: ApprovalDecision[]) => void | Promise<void>;
const noop: Resolve = () => {};

/** A chip's matching `ViewItem` (when the transcript's own View list carries
 *  one) plus its position in that chronologically-ordered list — the position
 *  is what `isNew` compares against the "seen through" pointer, rather than a
 *  fabricated per-render index. */
export interface ChipLookupEntry {
  item: ViewItem;
  index: number;
}

export interface RowHandlers {
  onResolveHostCommands?: Resolve;
  /** Open a View item (a version or the live head) in the side viewport, by key. */
  onOpenInView?: (key: string) => void;
  /** Keyed lookup of the conversation's View list — lets an inline chip show the
   *  same version/time metadata `ViewTimelineRail` shows for the same item. */
  chipLookup?: Map<string, ChipLookupEntry>;
  /** The matched item's index must exceed this to render its chip's NEW marker. */
  seenIndex?: number;
}

/** How a full-width row spaces itself from the one above: "none" for the first,
 *  flush to the top, "gap" for everything after it.
 *
 *  Process rows take no part in this. Every one of them lives inside a work log
 *  (`WORK_LOG_MIN_RUN` is 1), hung off its trunk by a `Branch`, and the trunk is
 *  what joins them — there is no longer a rail between loose rows to keep
 *  continuous across a gap. */
export type TopSpacing = "none" | "gap";

/** A process row's place on its log's trunk, for a row that is not told one — no
 *  row above it and none below. */
const LONE: BranchEdge = { first: true, last: true };

/** The one vertical rhythm inside an assistant turn, in grid steps: between its
 *  blocks (an answer passage, a work log, a View chip) and between the turn's own
 *  parts around them (the progress line, sources, a stop footer). Both used to say
 *  12px separately; they are one spacing, so a density change moves both.
 *
 *  8px rather than 12: the rows between these gaps are 24px now, and a gap half a
 *  row tall read as a paragraph break between things that are one turn. */
export const TURN_GAP = 2;

/** `TURN_GAP` as a margin. Tailwind needs the literal class, and keying the map by
 *  the constant's type makes changing one without the other a type error. */
const TURN_GAP_MARGIN: Record<typeof TURN_GAP, string> = { 2: "mt-2" };

/** Margin for a full-width row, or a work log — spacing always lives outside. */
export function fullWidthTop(top?: TopSpacing): string | undefined {
  return top && top !== "none" ? TURN_GAP_MARGIN[TURN_GAP] : undefined;
}

/** A chip's kindWord/meta/isNew, derived from its matching `ViewItem` (when the
 *  turn's own View list carries one) via the same helpers `ViewTimelineRail`
 *  uses for the identical data. No match (e.g. cold history whose item fell out
 *  of the list) falls back to `ViewChip`'s own icon-derived defaults. */
function chipMeta(
  lookup: Map<string, ChipLookupEntry> | undefined,
  seenIndex: number | undefined,
  key: string,
): { kindWord?: string; meta?: string; isNew: boolean } {
  const entry = lookup?.get(key);
  if (!entry) return { isNew: false };
  return {
    kindWord: classifyViewItem(entry.item).word,
    meta: viewItemVersionLabel(entry.item) ?? viewItemTimeLabel(entry.item),
    isNew: entry.index > (seenIndex ?? -1),
  };
}

/** Render one block group by kind. Host commands arrive as a group (consecutive blocks
 *  batched) so their cards keep one shared decision.
 *
 *  Approvals have no case here, and neither does a question still *waiting*: a parked run
 *  is answered in the dock that takes over the composer, not on the trunk (`ParkDock`, and
 *  `groupBlocks`'s `DOCKED`). An **answered** question does reach here — it is a result
 *  rather than something to act on, so it renders full-width like a View chip rather than
 *  on the trunk with the process. */
export function BlockRow(
  props: {
    group: BlockGroup;
    /** This group is the turn's live, trailing block. */
    active?: boolean;
    streaming?: boolean;
    forceOpen?: boolean;
    top?: TopSpacing;
    /** Where a process row sits on its work log's trunk. */
    edge?: BranchEdge;
    /** The row is arriving, so its branch traces in (`Branch trace`). */
    trace?: boolean;
  } & RowHandlers,
): JSX.Element {
  const g = () => props.group;
  return (
    <Switch>
      <Match when={g().kind === "text"}>
        {/* `data-live-answer` is what the transcript holds still while it streams. */}
        <div
          class={fullWidthTop(props.top)}
          data-live-answer={props.active && props.streaming ? "" : undefined}
        >
          <AnswerText
            text={(g().blocks[0] as TextBlock).text}
            active={props.active}
            streaming={props.streaming}
          />
        </div>
      </Match>
      <Match when={g().kind === "view_version"}>
        {(() => {
          const b = g().blocks[0] as ViewVersionBlock;
          const key = snapshotKey(b.snapshotId);
          const chip = () => chipMeta(props.chipLookup, props.seenIndex, key);
          return (
            <div class={fullWidthTop(props.top)}>
              <ViewChip
                icon={versionIcon(b.previewKind)}
                label={b.title || "Version"}
                kindWord={chip().kindWord}
                meta={chip().meta}
                isNew={chip().isNew}
                onOpen={() => props.onOpenInView?.(key)}
              />
            </div>
          );
        })()}
      </Match>
      <Match when={g().kind === "view_live"}>
        {(() => {
          const live = (g().blocks[0] as ViewLiveBlock).live;
          const chip = () =>
            chipMeta(props.chipLookup, props.seenIndex, LIVE_KEY);
          return (
            <div class={fullWidthTop(props.top)}>
              <ViewChip
                icon="play"
                label={live.title || "Live view"}
                live
                kindWord={chip().kindWord}
                meta={chip().meta}
                isNew={chip().isNew}
                onOpen={() => props.onOpenInView?.(LIVE_KEY)}
              />
            </div>
          );
        })()}
      </Match>
      {/* Only an *answered* one reaches here (`groupBlocks`), and only ever with
          something in it — but the card is guarded rather than trusting that, since an
          empty one would render a heading over nothing. */}
      <Match
        when={
          g().kind === "question" &&
          (g().blocks[0] as QuestionBlock).question.answers?.length
        }
      >
        <div class={fullWidthTop(props.top)}>
          <QuestionAnswerCard
            answers={(g().blocks[0] as QuestionBlock).question.answers ?? []}
          />
        </div>
      </Match>
      <Match when={g().kind === "thinking"}>
        <Branch
          edge={props.edge ?? LONE}
          trace={props.trace}
          lit={props.active}
        >
          <ReasoningBlock
            reasoning={(g().blocks[0] as ThinkingBlock).text}
            open={props.forceOpen}
            active={props.active}
            streaming={props.streaming}
          />
        </Branch>
      </Match>
      <Match when={g().kind === "tool"}>
        <Branch
          edge={props.edge ?? LONE}
          trace={props.trace}
          lit={props.active}
        >
          <ToolCallCard
            tool={(g().blocks[0] as ToolBlock).tool}
            open={props.forceOpen}
          />
        </Branch>
      </Match>
      <Match when={g().kind === "context"}>
        {/* On the trunk, because it happened in the turn's sequence — but never `active`:
            the trunk's light means "this is running now", and an injection is a settled
            fact the moment it exists. Lighting it would spend the one signal the
            interface delivers in light on something with no duration. */}
        <Branch edge={props.edge ?? LONE} trace={props.trace} chassis>
          <ContextInjectionCard
            injection={(g().blocks[0] as ContextBlock).injection}
            open={props.forceOpen}
          />
        </Branch>
      </Match>
      <Match when={g().kind === "review"}>
        {/* On the trunk, because it happened in the turn's sequence, and never `active`:
            the trunk's light means "the model is doing this now", and a review is the
            chassis answering for the operator — the opposite kind of event. */}
        <Branch edge={props.edge ?? LONE} trace={props.trace} chassis>
          <ReviewCard
            review={(g().blocks[0] as ReviewBlock).review}
            open={props.forceOpen}
          />
        </Branch>
      </Match>
      <Match when={g().kind === "host_command"}>
        <Branch
          edge={props.edge ?? LONE}
          trace={props.trace}
          lit={props.active}
        >
          <HostCommandCard
            commands={(g().blocks as HostCommandBlock[]).map((b) => b.command)}
            open={props.forceOpen}
            onSubmit={props.onResolveHostCommands ?? noop}
          />
        </Branch>
      </Match>
    </Switch>
  );
}
