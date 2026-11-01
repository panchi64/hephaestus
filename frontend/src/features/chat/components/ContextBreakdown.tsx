import { For, Show, createSignal, type JSX } from "solid-js";
import { compactCount, pct } from "~/lib/format";
import { Icon, Text } from "~/ui";
import type { ContextUsage, LastRequest } from "../model";
import {
  contextRows,
  foldMarker,
  type ContextDetail,
  type ContextRow,
  type FoldMarker,
} from "./contextRows";
import { LastRequestPanel } from "./LastRequestPanel";
import { StackedBar } from "./StackedBar";

/** The same severity the ring carries, on the panel's headline figure. It differs from
 *  the ring's map in one place: at rest the ring is `dim` (a gauge with nothing to say
 *  should recede) but the headline is `bright`, because a panel the operator
 *  deliberately opened is not competing for attention — they are already looking at it,
 *  and dimming the one number they opened it to read would be restraint pointed at the
 *  wrong thing. */
const HEADLINE_TONE = {
  nominal: "bright",
  warn: "warn",
  alert: "alert",
} as const;

const tokens = (n: number) => n.toLocaleString("en-US");

/** What the window is holding, and what is left of it.
 *
 *  The ring outside answers *how full*; this answers *full of what*, and the answers
 *  lead to different actions — a thread heavy with messages wants a compaction, one
 *  heavy with tool schemas wants fewer tools switched on, one heavy with tool *results*
 *  wants the tools that return too much. That is the whole reason the split is worth a
 *  click rather than a tooltip.
 *
 *  **Three rows, then as many as the operator asks for.** The groups are the reading;
 *  the itemisation beneath each is the decision, and it stays folded until it is wanted
 *  because a dozen rows at rest is a panel to study rather than one to glance at. Which
 *  rows exist at all is the backend's: it emits a segment only once it weighs something,
 *  so a fresh thread shows two lines and a long tool-heavy one grows the rest as they
 *  start costing the window. Nothing here renders a placeholder for a row that did not
 *  arrive. */
export function ContextBreakdown(props: {
  usage: ContextUsage;
  /** The last model request on its own, when the thread has produced one. Rendered
   *  beneath the split rather than on the composer's line, for the reason
   *  `LastRequestPanel` states: those figures are per-request and that line is
   *  cumulative. */
  lastRequest?: LastRequest | null;
}): JSX.Element {
  const rows = () => contextRows(props.usage);
  const fold = () => foldMarker(props.usage);
  const percent = () => props.usage.fraction * 100;
  return (
    <>
      <div class="flex items-baseline justify-between gap-3">
        <div class="flex items-baseline gap-2">
          {/* The hero value: sans and tabular, per the readout rule — a headline figure
              is the interface speaking, not the machine listing. The only thing that
              varies is its tone, and it carries the same severity the ring does, so the
              hue means here exactly what it means out there. */}
          <Text
            variant="readout"
            tone={HEADLINE_TONE[props.usage.level]}
            class="tabular-nums"
          >
            {pct(percent())}
          </Text>
          <Text variant="plate" tone="dim">
            of context used
          </Text>
        </div>
        <Text variant="micro" tone="dim">
          ~{compactCount(props.usage.used, true)} /{" "}
          {compactCount(props.usage.window, true)}
        </Text>
      </div>

      <Bar rows={rows()} fold={fold()} />

      {/* What the mark on the bar means, in words — the mark alone says "something
          happens here" without saying what, and a fold is not a thing to have to infer.
          Three states, because they lead to different decisions: room left before the
          fold, a fold that is about to fire, and a fold the operator has paused (where
          the mark is the room they are choosing to spend). */}
      <Show when={fold()}>
        {(mark) => (
          <Text variant="micro" tone="dim">
            <Show
              when={mark().active}
              fallback={`Folding is off for this thread — it would fold at ${pct(mark().pct)}.`}
            >
              <Show
                when={!mark().passed}
                fallback={`Past the ${pct(mark().pct)} fold point — the next turn folds.`}
              >
                Folds at {pct(mark().pct)} — about{" "}
                {compactCount(mark().headroomTokens, true)} tokens from here.
              </Show>
            </Show>
          </Text>
        )}
      </Show>

      <div class="flex flex-col">
        <For each={rows()}>{(row) => <Row row={row} />}</For>
      </div>

      <Show when={!props.usage.parts}>
        <Text variant="caption" tone="dim">
          The breakdown appears once a turn has run — the tool schemas and
          system prompt aren't in the stored transcript, so they're measured as
          a message is sent, then kept with the thread.
        </Text>
      </Show>

      {/* The one place the exact number lives. Everything above is rounded, because a
          gauge is read for its magnitude — but the operator deciding whether to compact
          wants the real figure, and it costs one dim line to give it to them. */}
      <Text variant="micro" tone="dim">
        {tokens(props.usage.used)} of {tokens(props.usage.window)} tokens
      </Text>

      <Show when={props.lastRequest}>
        {(request) => <LastRequestPanel request={request()} />}
      </Show>
    </>
  );
}

/** The fullness bar, split by what fills it.
 *
 *  Every row is drawn against the **window**, free space included, so the bar *is* the
 *  window: the filled run is the fraction the ring draws and the pale tail is the room
 *  left. A bar normalised to what's used would show a full-width strip on a 5%-full
 *  thread, which says the opposite of what the ring beside it says. */
function Bar(props: {
  rows: ContextRow[];
  /** Where this thread folds, when the backend reported a policy. */
  fold: FoldMarker | null;
}): JSX.Element {
  return (
    // `relative` so the fold mark can sit at its own fraction of the window. The
    // mark is a sibling of the bar rather than a child, which is what lets it be
    // taller than the bar and cross it — see below.
    <div class="relative w-full">
      <StackedBar
        segments={props.rows.map((row) => ({
          key: row.key,
          share: row.share,
          fill: row.fill,
        }))}
      />
      <Show when={props.fold}>
        {(mark) => (
          // Taller than the bar and crossing it, so it reads against the fill and against
          // the free tail alike — a notch *inside* the bar would have to be the panel's
          // own background colour to be visible, and would vanish the moment the fill
          // reached it. The bar is `h-1` (4px), so an 8px mark lifted 2px straddles it
          // evenly, on the spacing scale.
          //
          // `-translate-x-1/2` centres the mark *on* its fraction rather than starting it
          // there: at 100% a left-edge-aligned mark falls entirely outside the track, and
          // everywhere else it sits half its width late.
          //
          // Luminance carries active/paused, never hue: hue is severity here and a fold
          // is not a severity.
          <div
            aria-hidden="true"
            class={`absolute -top-0.5 h-2 w-px -translate-x-1/2 ${
              mark().active ? "bg-bright" : "bg-dim"
            }`}
            style={{ left: `${mark().pct}%` }}
          />
        )}
      </Show>
    </div>
  );
}

/** One group: its swatch, its figures, and — where the backend itemised it — the rows
 *  it is made of, one click away.
 *
 *  A row with no detail is rendered as a plain line rather than an inert button, so the
 *  chevron never promises something that isn't there. */
function Row(props: { row: ContextRow }): JSX.Element {
  const [open, setOpen] = createSignal(false);
  const expandable = () => props.row.detail.length > 0;
  return (
    <div>
      <button
        type="button"
        disabled={!expandable()}
        aria-expanded={expandable() ? open() : undefined}
        onClick={() => setOpen(!open())}
        class="group flex w-full items-center gap-2 py-1 text-left enabled:cursor-pointer"
      >
        {/* The swatch is square, like `StatusDot`'s 8px block: at this size the
            control radius is 37% of the mark and turns a legend swatch into a
            blob. */}
        <span class="flex size-3 shrink-0 items-center justify-center">
          <Show
            when={expandable()}
            fallback={<span class={`size-2 ${props.row.fill}`} />}
          >
            <Icon
              name={open() ? "chevron-down" : "chevron-right"}
              size={12}
              class="text-dim group-hover:text-bright"
            />
          </Show>
        </span>
        <Show when={expandable()}>
          <span class={`size-2 shrink-0 ${props.row.fill}`} />
        </Show>
        <Text variant="label" tone="default" class="min-w-0 truncate">
          {props.row.label}
        </Text>
        <span class="ml-auto flex shrink-0 items-baseline gap-3">
          <Text variant="micro" tone="dim" class="tabular-nums">
            ~{compactCount(props.row.tokens, true)}
          </Text>
          {/* Fixed-width so the column reads down as a column: a breakdown is scanned
              vertically, and shares that start at different x-positions have to be
              read one at a time. */}
          <Text variant="micro" tone="dim" class="w-11 text-right tabular-nums">
            {pct(props.row.share, 1)}
          </Text>
        </span>
      </button>

      <Show when={open()}>
        <div class="mb-1 ml-5 flex flex-col border-l border-line pl-3">
          <For each={props.row.detail}>{(item) => <Detail item={item} />}</For>
        </div>
      </Show>
    </div>
  );
}

/** One line item inside a group. Dimmer and countable: the figures are already ranked
 *  by size, so what the operator needs beside them is the population behind the number
 *  — 22K of schemas is a lot of text, 22K across 68 tools is a catalog to prune. */
function Detail(props: { item: ContextDetail }): JSX.Element {
  return (
    <div class="flex items-baseline gap-3 py-1">
      <Text variant="micro" tone="dim" class="min-w-0 truncate">
        {props.item.label}
      </Text>
      <span class="ml-auto flex shrink-0 items-baseline gap-3">
        <Show when={props.item.count !== null}>
          <Text variant="micro" tone="dim" class="tabular-nums">
            {props.item.count} tools
          </Text>
        </Show>
        <Text variant="micro" tone="default" class="tabular-nums">
          ~{compactCount(props.item.tokens, true)}
        </Text>
        {/* The empty column the group row spends on its share, so a child's figure
            sits directly under its parent's rather than under the percentage beside
            it — the two columns mean different things and reading down a column is
            the only reason to have columns. */}
        <span class="w-11" aria-hidden="true" />
      </span>
    </div>
  );
}
