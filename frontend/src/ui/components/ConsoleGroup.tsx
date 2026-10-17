import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

export interface ConsoleGroupProps {
  /** The engraved legend in the header band. Sentence or upper — `plate` uppercases. */
  label: string;
  /** Readout at the right of the header band: a count, a clock, a status flag. */
  right?: JSX.Element;
  /** Flush ruled rows. Give them no horizontal padding of their own — the group
   *  supplies it, so every row's rules land on the same two columns. */
  children: JSX.Element;
  /** Rows run edge to edge and carry their own padding — for a `divide-y` list, whose
   *  rules must reach the frame on both sides. Use the same `px-2` gutter the header
   *  does, or the legend stops lining up with the rows it names. */
  flush?: boolean;
  class?: string;
}

/** A console group (§10.14): a frame, a header band, and flush ruled rows.
 *
 *  **This is the one shell allowed to be a box.** §7 makes a border the last resort
 *  and this is the case it is last for: the rows inside are flush by construction, so
 *  there is no gap for space to work in, and they must read as set *into* the panel
 *  rather than lifted off it, so surface value cannot do it either. Both of the other
 *  devices are unavailable, which is exactly when a real border is justified.
 *
 *  **What goes in one:** ruled rows and fixed cells — content read by scanning a
 *  column, at the §3 tabular density. **What does not:** prose, forms, or a stack of
 *  cards. `Panel` is still the default container and stays the default; reach for this
 *  only when the content is genuinely a readout and the frame is what says where that
 *  readout begins and ends.
 *
 *  Square by construction (§7): anything in a ruled grid takes `radius-0`, and rounding
 *  the frame would leave the rows inside it mitred against a curve.
 */
export function ConsoleGroup(props: ConsoleGroupProps): JSX.Element {
  const [local] = splitProps(props, [
    "label",
    "right",
    "children",
    "flush",
    "class",
  ]);
  return (
    // `heph-framed`: on a page carrying the dot field this surface goes glass,
    // so the dots behind a group of readouts arrive blurred rather than running
    // under the rows (§11.1). Inert on every other page.
    <div class={cx("heph-framed border border-line bg-surface", local.class)}>
      <div class="flex items-center gap-2 border-b border-line px-2 py-1">
        <Text variant="plate" tone="dim" class="min-w-0 truncate">
          {local.label}
        </Text>
        {/* `min-w-0`, not `shrink-0`: a readout that truncates (a sub-agent's handle)
            can only do so if its slot is allowed to give way. A short count gives
            way least — flex shrinks each side in proportion to its content. */}
        <Show when={local.right}>
          <div class="ml-auto flex min-w-0 items-center gap-2">
            {local.right}
          </div>
        </Show>
      </div>
      {/* An EVEN gutter, and the TIGHT one (`p-1`), not `px-2 py-1`. The body holds a
          block — a grid of cells, a list of rows — and an inset that is 8px at the
          sides and 4px top and bottom reads as the block having slipped upward inside
          its frame. Equalizing downward rather than upward is the §3 answer: this is a
          readout, the content inside it carries its own cell padding, and a second
          8px margin around it only pushes the frame away from what it encloses.

          The header band keeps its own `px-2 py-1` — it is one line of type set into
          the frame, not a block, and its legend was never flush with the cell text
          below it anyway (a cell pays its own gutter on top of this one). */}
      <div class={local.flush ? undefined : "p-1"}>{local.children}</div>
    </div>
  );
}
