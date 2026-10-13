import { For, Show, createMemo, untrack, type JSX } from "solid-js";
import { Collapse, cx } from "~/ui";
import { createArrivalTracker } from "../arrivals";
import type { WorkLogEntry } from "../blocks";
import { latestReason, workShape } from "../workShape";
import {
  BlockRow,
  fullWidthTop,
  type RowHandlers,
  type TopSpacing,
} from "./BlockRow";
import { type BranchEdge } from "./Branch";
import { SchematicContext } from "./ProcessRow";
import { WorkLogHeader } from "./WorkLogHeader";
import { WorkLogLatest } from "./WorkLogLatest";

/** The turn's account of what it did: one log per turn, in order, under one
 *  header.
 *
 *  The header leads with the run's latest *reason* — the model's narration — and,
 *  while shut, hangs the latest step beneath it in the rows' own `glyph · Label ·
 *  detail` anatomy. See `WorkLogHeader` and `WorkLogLatest`.
 *
 *  **Some rows the fold may not hide, and they stay in place rather than outside.**
 *  A call in flight, a failure, a screenshot and a refusal are all pinned
 *  (`pinsRunInline` in `blocks.ts`), and they used to be pinned by being *ejected*
 *  from the log — which ended it and started a second one underneath with the same
 *  name. A turn with one failed call rendered as three separate items the operator
 *  had to reassemble into one sequence. The promise was only ever that such a row
 *  stays visible; it is kept here by rendering it outside the fold's collapse while
 *  leaving it where it happened.
 *
 *  That is why this walks one list and gates each row individually instead of
 *  wrapping the whole run in one `Collapse`: the fold has to be able to close
 *  around a pinned row without closing over it. */
export function WorkLog(
  props: {
    entries: WorkLogEntry[];
    /** Open state is owned by the turn (keyed by a stable id) so it survives the
     *  remount when a streaming delta rebuilds the layout — a local signal here
     *  would reset on every new block. */
    open: boolean;
    onToggle: () => void;
    forceOpen?: boolean;
    top?: TopSpacing;
    /** Groups with a call in flight, for the trunk's light. Read here because a live
     *  call is now pinned *inside* the log rather than lifted out of it. */
    activeIds?: ReadonlySet<string>;
    streaming?: boolean;
  } & RowHandlers,
): JSX.Element {
  /* Two readings of the run, over two different sets.
     The reason is read over *everything*: a live call is pinned, and its
     narration is the freshest account of what the run is doing right now.
     The latest step and the count are read over the *fold* only: a pinned row is
     already on screen, so repeating it as the indented row would show it twice,
     and counting it among what "opening this would reveal" overstates the fold —
     the operator reads `5 steps`, opens it, and two rows appear. */
  const summary = createMemo(() =>
    latestReason(props.entries.map((e) => e.group)),
  );
  const folded = createMemo(() =>
    workShape(props.entries.filter((e) => !e.pinned).map((e) => e.group)),
  );
  /* Every row is on screen already, so the chevron would open onto nothing. */
  const foldable = createMemo(() => folded().steps > 0);
  /** Whether the foldable segments are showing. `forceOpen` is the turn's expand-all. */
  const open = () => props.open || props.forceOpen === true;

  /* Consecutive rows of the same kind, in order.
   *
   *  A stretch of foldable rows shares **one** `Collapse` — the shape the log had
   *  before a pinned row was allowed to sit inside it, and the reason matters: a
   *  `Collapse` per row is a height measurement and a transition per row, so a
   *  forty-call turn folded in forty independent animations where one would do.
   *
   *  Segments and rows are iterated by string key, not by object: the layout mints
   *  fresh entries on every recompute, and a reference-keyed `<For>` would remount
   *  every row (and replay its arrival) each time a block lands. */
  const segments = createMemo(() => {
    const out: { key: string; pinned: boolean; ids: string[] }[] = [];
    for (const entry of props.entries) {
      const tail = out[out.length - 1];
      const id = entry.group.id;
      if (tail && tail.pinned === entry.pinned) tail.ids.push(id);
      else
        out.push({
          key: `${entry.pinned ? "p" : "f"}:${id}`,
          pinned: entry.pinned,
          ids: [id],
        });
    }
    return out;
  });
  const segmentKeys = createMemo(() => segments().map((s) => s.key));
  const segmentByKey = createMemo(
    () => new Map(segments().map((s) => [s.key, s] as const)),
  );
  const entryById = createMemo(
    () => new Map(props.entries.map((e) => [e.group.id, e] as const)),
  );

  /* The ids this update added to the log. A row traces in only when it mounts as one
     of them during a stream, so a settled call moving into the fold or the latest
     slot, a log being opened, and a thread loading from history all stay still. */
  const arrivals = createArrivalTracker(
    untrack(() => props.entries.map((e) => e.group.id)),
  );
  const fresh = createMemo(() =>
    arrivals(props.entries.map((e) => e.group.id)),
  );
  const arrives = (id: string): boolean =>
    untrack(() => props.streaming === true && fresh().has(id));

  /* What hangs off the trunk right now, in drawn order: the latest step (shut
     only), then every entry the fold shows — all of them open, the pinned ones
     shut. The last turns the trunk into its elbow, so the line runs unbroken
     from the header to the last row and stops there. */
  const drawn = createMemo(() => {
    const ids: string[] = [];
    const latestId = folded().latestId;
    if (!open() && latestId) ids.push(latestId);
    for (const e of props.entries) if (open() || e.pinned) ids.push(e.group.id);
    return ids;
  });
  const edgeOf = (id: string): BranchEdge => {
    const ids = drawn();
    return { first: ids[0] === id, last: ids[ids.length - 1] === id };
  };

  return (
    <div class={cx("relative", fullWidthTop(props.top))}>
      {/* The trunk's first length, from the foot of the header's chevron (`py-1`
          + 2px of the 16px line box + the 12px glyph = 18px) to the foot of the
          header row (24px), where the first branch takes over. Drawn here rather
          than by that branch because it may sit inside a `Collapse`, which clips. */}
      <Show when={drawn().length > 0}>
        <span
          aria-hidden="true"
          class="pointer-events-none absolute top-4.5 left-3.5 h-1.5 w-px bg-line"
        />
      </Show>
      <WorkLogHeader
        summary={summary()}
        steps={folded().steps}
        open={props.open}
        foldable={foldable()}
        onToggle={() => props.onToggle()}
      />
      {/* Keyed on the block, and open only while shut: open, the same step is the
          last row of the list below.

          The step itself is read through the inner `Show`, not captured here: a
          keyed child runs untracked, and the same block can change in place — a
          folded review goes from `reviewing` to its decision under one id — so a
          value snapshotted at mount would sit stale for the rest of the turn. */}
      <SchematicContext.Provider value={true}>
        <Show when={!open() && folded().latestId} keyed>
          {(id: string) => {
            const trace = arrives(id);
            return (
              <Show when={folded().latest}>
                {(step) => (
                  <WorkLogLatest
                    step={step()}
                    edge={edgeOf(id)}
                    trace={trace}
                  />
                )}
              </Show>
            );
          }}
        </Show>
        <For each={segmentKeys()}>
          {(key) => {
            const ids = () => segmentByKey().get(key)?.ids ?? [];
            const rows = (
              <For each={ids()}>
                {(id) => {
                  const trace = arrives(id);
                  return (
                    <Show when={entryById().get(id)}>
                      {(entry) => (
                        <BlockRow
                          group={entry().group}
                          active={props.activeIds?.has(id)}
                          streaming={props.streaming}
                          trace={trace}
                          edge={edgeOf(id)}
                          forceOpen={props.forceOpen}
                          onResolveHostCommands={props.onResolveHostCommands}
                          onOpenInView={props.onOpenInView}
                          chipLookup={props.chipLookup}
                          seenIndex={props.seenIndex}
                        />
                      )}
                    </Show>
                  );
                }}
              </For>
            );
            /* `Collapse` rather than a bare `Show`: folding a stretch of rows away
               instantly takes everything below it with it, which is a jump the reader
               has to recover their place after. A pinned segment skips it entirely —
               it is never the thing being folded. The key carries the kind, so a
               segment never changes kind in place. */
            return key.startsWith("p:") ? (
              rows
            ) : (
              <Collapse open={open()}>{rows}</Collapse>
            );
          }}
        </For>
      </SchematicContext.Provider>
    </div>
  );
}
