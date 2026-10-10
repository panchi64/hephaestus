import { Show, type JSX } from "solid-js";
import { Text } from "~/ui";

/** One ruled row of a fold's panel: an engraved legend over what it names.
 *
 *  Its own module because both halves of the panel draw it — `CompactionTurn` for the
 *  fold's figures, `CompactionSummary` for each section — and neither should have to
 *  import the other to reach it.
 *
 *  Stacked rather than set in two columns, because the legends are short and the values
 *  are not — a fixed label column wide enough for "Context" leaves a summary paragraph
 *  reading in a gutter on a half-width panel. The `plate` step does the work a column
 *  would: it names a region, which is exactly what §4 keeps it for. */
export function CompactionRow(props: {
  legend: string;
  /** Figures that belong to this row, set at the end of its legend line — or
   *  `undefined` when there are none, which drops the slot entirely.
   *
   *  A row rather than a row of its own: two token counts are not worth a `plate` legend
   *  and a line of their own, and read better beside the thing they measure than as a
   *  separate readout the operator has to relate back to it. */
  readout?: JSX.Element;
  children: JSX.Element;
}): JSX.Element {
  return (
    <div class="space-y-1 px-2 py-2">
      <div class="flex items-baseline justify-between gap-3">
        <Text variant="plate" tone="dim" class="min-w-0 truncate">
          {props.legend}
        </Text>
        <Show when={props.readout}>
          <Text variant="micro" tone="dim" class="shrink-0 tabular-nums">
            {props.readout}
          </Text>
        </Show>
      </div>
      {props.children}
    </div>
  );
}
