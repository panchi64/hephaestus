import { Show, children, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import type { IconName } from "../icons/registry";
import { Icon } from "../primitives/Icon";

/** One cell's box, shared with every trigger that has to sit in a `StatusBar` as a
 *  cell of its own — `Select cell`, `Combobox cell`. Exported rather than restated so
 *  a picker in the bar and a plain cell beside it cannot drift a pixel apart: the bar
 *  reads as one ruled strip only while every cell has the same height and gutter.
 *
 *  Mono and snapping (§8): every cell is a value or a setting in the machine's own
 *  line, so the tone change on hover is a cut, not a fade. `lowercase` is the bar's
 *  voice — the text beneath the card is a line of telemetry, and a sentence-case
 *  label there ("Auto-review", "Stats") reads as a heading over the row. */
export const STATUS_CELL_CLASS =
  "ody-status-cell flex h-6 items-center gap-1.5 px-2.5 font-mono text-meta lowercase whitespace-nowrap transition-none";

/** A cell that acts: the text tone at rest, bright on a raised fill under the pointer.
 *  A disabled one drops to the line colour — present, so the row doesn't reflow, and
 *  plainly not on offer. */
export const STATUS_CELL_ACTION_CLASS =
  "cursor-pointer hover:bg-raised hover:text-bright disabled:cursor-not-allowed disabled:text-line-strong disabled:hover:bg-transparent";

/** A resolved child that renders something — a declined `Show` resolves to nothing. */
const present = (child: unknown): boolean =>
  child !== null && child !== undefined && child !== false && child !== "";

export interface StatusBarProps {
  /** Cells from the left edge — what the next message *is* (attach, its level, its
   *  model). Each direct child is a cell and takes a hairline on its right. */
  start?: JSX.Element;
  /** Cells from the right edge — the state of the thread it goes into. Each direct
   *  child takes a hairline on its left. */
  end?: JSX.Element;
  /** How the bar meets what is around it.
   *
   *  `joined` (default) is the underside of a bordered unit: a `line-strong` rule on
   *  top only, the unit's own frame closing the other three sides. `box` is a bar
   *  standing on its own — the same rule on all four sides — for when the unit it
   *  belongs to has given up its slot (the composer, while the approval dock holds it). */
  edge?: "joined" | "box" | "block";
  /** `ruled` (default): `line` hairlines between cells, the text tone. `recede`: a
   *  sunken fill, no dividers, dim mono at a 28px row that comes up to the text tone
   *  only while the pointer or focus is in the strip — for a strip that should sit
   *  *behind* what it serves (the composer's, the rail's). Its rules are `line`
   *  hairlines rather than `line-strong`, and `block` draws one above and below. */
  variant?: "ruled" | "recede";
  /** Keep the bar to one row: cells that can shrink (a truncating picker) give way
   *  instead of the row wrapping. */
  nowrap?: boolean;
  class?: string;
}

/**
 * **A ruled row of fixed cells** under a unit it reports on — the composer's
 * status bar. Cells sit edge to edge at one height, separated by `line` hairlines,
 * which is the one kind of border §7 keeps: a ruled strip of machine values, where the
 * rule is what makes a run of short readouts scan as a row of cells rather than as
 * words with odd gaps between them.
 *
 * It is not an `InstrumentBand` and deliberately so. A band is a readout —
 * label-over-value cells the operator reads; this is a control strip whose cells are
 * mostly things to press, one line tall, in the same mono as the readouts beside them.
 *
 * **It wraps rather than overflowing.** The start group keeps its place and its cells
 * wrap within it; the end group is pushed right with `ml-auto` and drops to its own
 * line still right-aligned when the two no longer fit side by side. A cell that holds
 * something long (a model id) should cap and truncate inside itself, so it gives way
 * before its neighbours are pushed onto a line of their own.
 */
export function StatusBar(props: StatusBarProps): JSX.Element {
  const [local] = splitProps(props, [
    "start",
    "end",
    "edge",
    "variant",
    "nowrap",
    "class",
  ]);
  const wrap = () => (local.nowrap ? "flex-nowrap" : "flex-wrap");
  const recede = () => local.variant === "recede";
  const rule = () => (recede() ? "border-line" : "border-line-strong");
  const edgeClass = () =>
    local.edge === "box"
      ? "border"
      : local.edge === "block"
        ? "border-y"
        : "border-t";
  // Each slot resolved once: a JSX prop is a getter that rebuilds its elements on every
  // read, and a group tested for emptiness and then rendered would mount twice.
  const start = children(() => local.start);
  const end = children(() => local.end);
  // Counted, not truth-tested: a group of `Show`s that all declined resolves to an
  // empty array, which is truthy and would draw a group with nothing in it.
  const hasStart = () => start.toArray().some(present);
  const hasEnd = () => end.toArray().some(present);
  // Nothing in either group draws no bar at all — an empty frame is a line with
  // nothing to rule off.
  return (
    <Show when={hasStart() || hasEnd()}>
      <div
        class={cx(
          "flex items-stretch justify-between text-text",
          wrap(),
          edgeClass(),
          rule(),
          recede() && "ody-status-recede",
          local.class,
        )}
      >
        {/* The rules are set from the group onto its direct children, so a caller hands
          over plain cells and a cell never has to know which end of the bar it is at.
          A receding strip draws none: its cells are told apart by spacing alone. */}
        <Show when={hasStart()}>
          <div
            class={cx(
              "flex min-w-0 items-stretch",
              wrap(),
              recede() ? "flex-1" : "[&>*]:border-r [&>*]:border-line",
            )}
          >
            {start()}
          </div>
        </Show>
        <Show when={hasEnd()}>
          <div
            class={cx(
              "ml-auto flex min-w-0 items-stretch justify-end",
              wrap(),
              local.nowrap && "shrink-0",
              !recede() && "[&>*]:border-l [&>*]:border-line",
            )}
          >
            {end()}
          </div>
        </Show>
      </div>
    </Show>
  );
}

export interface StatusCellProps extends Omit<
  JSX.ButtonHTMLAttributes<HTMLButtonElement>,
  "type"
> {
  /** Makes the cell a button. Without it the cell is a plain readout. */
  onClick?: JSX.EventHandler<HTMLButtonElement, MouseEvent>;
  /** Brightens the resting tone — for a cell whose panel is open, or whose value is
   *  live. Brightness carries the state, never hue (§5). */
  active?: boolean;
  class?: string;
  children: JSX.Element;
}

export interface StatusFieldProps {
  value: string;
  onInput: (value: string) => void;
  placeholder: string;
  "aria-label": string;
  /** A glyph at the head of the cell. */
  leading?: IconName;
  /** The key that focuses the field, shown dim at its end (`/`). Binding it is the
   *  caller's. */
  kbd?: string;
  /** Escape in the field. */
  onEscape?: () => void;
  ref?: (el: HTMLInputElement) => void;
  class?: string;
}

/** A text field as a cell of a `StatusBar` — the rail's search. It takes the room the
 *  start group has left, in the cell's own mono at the cell's height, with no box of its
 *  own: the strip is already the control. */
export function StatusField(props: StatusFieldProps): JSX.Element {
  return (
    <label
      class={cx(STATUS_CELL_CLASS, "min-w-0 flex-1 cursor-text", props.class)}
    >
      <Show when={props.leading}>
        {(name) => <Icon name={name()} size={16} class="shrink-0" />}
      </Show>
      <input
        ref={(el) => props.ref?.(el)}
        type="text"
        value={props.value}
        placeholder={props.placeholder}
        aria-label={props["aria-label"]}
        onInput={(e) => props.onInput(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key !== "Escape") return;
          e.preventDefault();
          props.onEscape?.();
        }}
        class="min-w-0 flex-1 bg-transparent font-mono text-meta normal-case outline-none"
      />
      <Show when={props.kbd}>
        {(k) => (
          <kbd aria-hidden="true" class="shrink-0 font-mono opacity-70">
            {k()}
          </kbd>
        )}
      </Show>
    </label>
  );
}

/** One cell of a `StatusBar`: a readout, or — given `onClick` — a button that reads
 *  like one until it is pointed at. */
export function StatusCell(props: StatusCellProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "onClick",
    "active",
    "class",
    "children",
  ]);
  return (
    <Show
      when={local.onClick}
      fallback={
        <span class={cx(STATUS_CELL_CLASS, local.class)}>{local.children}</span>
      }
    >
      <button
        type="button"
        class={cx(
          STATUS_CELL_CLASS,
          STATUS_CELL_ACTION_CLASS,
          // One tone class or the other, never both: two `text-*` utilities on one
          // node resolve by stylesheet order, not by which was meant.
          local.active ? "bg-raised text-bright" : "text-text",
          local.class,
        )}
        onClick={(e) => local.onClick?.(e)}
        {...rest}
      >
        {local.children}
      </button>
    </Show>
  );
}
