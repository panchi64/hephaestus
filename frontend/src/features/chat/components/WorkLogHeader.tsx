import { Show, type JSX } from "solid-js";
import { Text } from "~/ui";
import { ProcessRow } from "./ProcessRow";

/** The node between the header's name and its summary — a 4px hard-cornered
 *  square, the registration-mark vocabulary (§9) rather than another `·`.
 *
 *  Drawn in CSS rather than as a glyph so it centres on the text's midline in
 *  every font, and so it reads as a *mark* — this row's own anchor — rather than
 *  as the punctuation the segments inside a row are separated by. */
function NodeSep(): JSX.Element {
  return (
    <span aria-hidden="true" class="size-1 shrink-0 bg-current text-dim" />
  );
}

/** The collapsed work log's own line: why the run is doing what it does, and how
 *  big it is.
 *
 *  **It leads with the model's reason, not with its last act.** The row beneath
 *  it (`WorkLogLatest`) names the act in the rows' own anatomy; this line carries
 *  the `narration` the model wrote for the most recent call that had one. Two
 *  earlier headlines were tried and both reported the wrong thing: `7 Steps` and
 *  `Read ×4 · Web search · +2` reported *magnitude* where the operator reads for
 *  identity, and `Read agent.py` gave identity without intent — the question a
 *  turn read back is almost always asking is what it was *for*.
 *
 *  It also moves while the turn runs, snapping rather than easing — mono, the
 *  machine register (§8). No throbber sits here: `TurnProgressRail` is where the
 *  live phase is spoken, once.
 *
 *  The step count is the run's size, dim and pinned right where a card puts its
 *  elapsed figure — metadata, not the headline. "Is opening this worth it" is
 *  the one question a bare magnitude does answer honestly. */
export function WorkLogHeader(props: {
  /** The run's most recent reason; absent when nothing in it gave one, and the
   *  header then reads as its name alone rather than inventing a sentence. */
  summary?: string;
  /** How many rows are actually behind the fold.
   *
   *  Not the size of the whole run. A pinned row — a failure, a live call, a
   *  screenshot — sits inside the log rather than beside it, so the run and the
   *  fold are not the same set, and counting the run would promise rows that are
   *  already on screen. */
  steps: number;
  open: boolean;
  /** False when every row is pinned: there is nothing to open onto, so the header
   *  keeps its summary and drops the chevron rather than offering an empty fold. */
  foldable: boolean;
  onToggle: () => void;
  /** Open the turn's call trace in the stats dialog. */
  onTrace?: () => void;
}): JSX.Element {
  return (
    <ProcessRow
      open={props.open}
      foldable={props.foldable}
      onToggle={props.onToggle}
      label="Work log"
      /* The summary is the segment that truncates, and it is the one worth
         reading — so the whole sentence is a hover away. */
      title={props.summary}
      /* From two steps up. A one-step fold's latest row already *is* its only
         row, so a count beside it would say the same thing twice — and "1 steps"
         is the shape that gives a generated readout away. */
      trailing={
        <>
          <Show when={props.steps > 1}>
            <Text variant="micro" tone="dim" class="tabular-nums">
              {`${props.steps} steps`}
            </Text>
          </Show>
          <Show when={props.onTrace}>
            {(open) => (
              <button
                type="button"
                aria-label="Open this turn's call trace"
                /* The row itself toggles the fold. */
                onClick={(e) => {
                  e.stopPropagation();
                  open()();
                }}
              >
                <Text variant="micro" tone="dim" class="hover:text-bright">
                  Trace
                </Text>
              </button>
            )}
          </Show>
        </>
      }
    >
      <Show when={props.summary}>
        {(summary) => (
          <>
            <NodeSep />
            {/* The only segment allowed to shrink (`ProcessRow`'s label rule). */}
            <Text variant="micro" tone="default" class="min-w-0 truncate">
              {summary()}
            </Text>
          </>
        )}
      </Show>
    </ProcessRow>
  );
}
