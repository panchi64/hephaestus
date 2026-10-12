import { Dynamic } from "solid-js/web";
import { For, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Marquee } from "./Marquee";
import { type Status } from "./StatusDot";

export interface AnnunciatorCell {
  /** The engraved legend. */
  label: string;
  status: Status;
  /** Why it is lit — rendered as the cell's second line. */
  detail?: string;
  /** Where to go and fix it. Makes the cell a link. */
  href?: string;
}

export interface AnnunciatorGridProps {
  cells: AnnunciatorCell[];
  class?: string;
}

/** How many columns to lay `n` cells out in, given the most that tier can take.
 *
 *  **The point is to have no ragged tail.** A fixed count leaves seven capabilities
 *  sitting 5 + 2, and the three dead positions read as a row that failed to finish
 *  rather than as the end of the panel. Filling the rows as evenly as possible instead
 *  puts seven in one row of seven where there is room, and 4 + 3 where there is not.
 *
 *  It cannot always reach zero — nine cells at a cap of eight is 5 + 4 whatever is
 *  chosen — so this minimizes the gap rather than promising none. The alternative that
 *  *would* promise none is letting the last row's cells stretch, and that is rejected:
 *  the columns would stop aligning between rows, which is the ruled structure §7 keeps
 *  the borders for in the first place.
 *
 *  Pure arithmetic over a count — no measurement, no layout read. */
function columnsFor(n: number, max: number): number {
  if (n <= 0) return 1;
  return Math.ceil(n / Math.ceil(n / max));
}

/** A status that is asking for the operator. `idle`/`live`/`nominal`/`info` are not
 *  conditions — they are the panel doing its job, and lighting a cell for them is how
 *  an annunciator turns back into the flat band it replaced. */
function isLit(status: Status): boolean {
  return status === "warn" || status === "alert";
}

/** Caution & warning (§10.15): one cell per capability, most of them dark.
 *
 *  **The operator reads this panel by looking for the lit one.** That is the whole
 *  design and the reason it replaced a flat strip of equally-bright cells: a band has
 *  to be read left to right every time, where a matrix with one amber cell in it has
 *  already answered the question from across the room. §1.1 — volume is hierarchy —
 *  applied to a region that was violating it.
 *
 *  **A cell is two lines: the legend, and why it is lit.** The second line is reserved
 *  whether or not the cell is lit, so a capability degrading changes a cell's contents
 *  and never its size — the grid cannot reflow under the operator. The reason used to
 *  be set in a list beneath the grid, which restated every legend in order to say the
 *  thing the cell it named had room for, and grew the panel by a line per fault.
 *
 *  **Caution is a tint; alert is a solid fill.** That is the §12 rule doing real work:
 *  with the reason occupying the second line there is no status *word* left, so the two
 *  severities would otherwise be told apart by hue alone. Fill weight is a luminance
 *  difference — it survives any colour vision, and it is what an actual caution-and-
 *  warning panel does, where a master alarm does not merely glow a different colour
 *  from a caution. Lit and unlit stay distinguishable by a third, independent channel:
 *  an unlit cell's second line is empty.
 *
 *  Square, ruled, `space-1`/`space-2` — the tabular-density exception of §3, and the
 *  ruled-grid border of §7.
 */
export function AnnunciatorGrid(props: AnnunciatorGridProps): JSX.Element {
  const [local] = splitProps(props, ["cells", "class"]);

  const n = () => local.cells.length;

  return (
    // Collapsed borders rather than `gap-px` over a filled container. A filled container
    // paints its own background through any empty tail, which reads as one wide blank
    // cell rather than as the end of the grid. Overlapping each cell's own border by a
    // pixel gives the same single hairline and leaves an empty position genuinely empty.
    //
    // The column counts are set per breakpoint as custom properties and applied by
    // `.ody-annunciator` in theme.css — see `columnsFor`.
    <div
      class={cx("ody-annunciator", local.class)}
      style={{
        "--ann-cols-sm": columnsFor(n(), 4),
        "--ann-cols-lg": columnsFor(n(), 6),
        "--ann-cols-xl": columnsFor(n(), 8),
      }}
    >
      <For each={local.cells}>
        {(cell) => {
          const on = () => isLit(cell.status);
          const fill = () =>
            cell.status === "alert"
              ? "bg-alert text-bg"
              : "bg-warn/10 text-warn";
          return (
            <Dynamic
              component={cell.href ? "a" : "div"}
              href={cell.href}
              class={cx(
                "-mt-px -ml-px flex min-w-0 flex-col gap-0.5 border border-line px-2 py-1.5 transition-none",
                on()
                  ? fill()
                  : "bg-annunciator-dark text-line-strong hover:text-dim",
                cell.href && "cursor-pointer",
              )}
            >
              <Text variant="plate" class="truncate text-current">
                {cell.label}
              </Text>
              {/* The reason scrolls rather than truncating, because it is the one
                  thing in the cell the operator actually has to finish reading — a
                  clipped "VM not ready — disa…" is the half that does not say what to
                  do. `Marquee` moves only when the text genuinely overflows its column
                  and holds still under reduced motion, so a panel at rest stays at
                  rest. The legend above it truncates instead: it is a name, and a name
                  that slides is unreadable as an index. */}
              <Marquee class="min-w-0" speed={24}>
                <Text variant="micro" class="text-current">
                  {(on() && cell.detail) || " "}
                </Text>
              </Marquee>
            </Dynamic>
          );
        }}
      </For>
    </div>
  );
}
