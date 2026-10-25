import { Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";

/** What a rail row's glyph column says. `waiting` is the action tier's echo: the run is
 *  parked on the operator. `done`, `current` and `pending` are a checklist's states. */
export type RailRowState =
  | "idle"
  | "queued"
  | "running"
  | "waiting"
  | "failed"
  | "done"
  | "current"
  | "pending";

export interface RailRowProps {
  state?: RailRowState;
  selected?: boolean;
  /** Something happened here the operator has not seen: a bright square, a bright title. */
  unread?: boolean;
  /** The title — text, or a typewriter revealing a new name. */
  title: JSX.Element;
  /** A readout that stays put (a clock, a tag, a failure word). */
  readout?: JSX.Element;
  /** A quiet readout the row's menu replaces under the pointer (the thread's age). */
  age?: string;
  /** Small markers before the readout (a pin). */
  marks?: JSX.Element;
  /** The row's `···` menu trigger, revealed on hover/focus in the readout's slot. */
  menu?: JSX.Element;
  menuOpen?: boolean;
  onOpen: () => void;
  onContextMenu?: (e: MouseEvent) => void;
  /** Rows nested under this one, folded by a chevron in the row's left gutter. Absent
   *  when there is nothing to fold, and then no chevron is drawn. */
  fold?: { open: boolean; onToggle: () => void; label: string };
  class?: string;
}

/**
 * **One square row of the rail** (direction C): an 8px glyph column carrying the row's
 * state, the title, and a mono readout. Selected is the raised fill plus a 2px bright
 * inset edge; a waiting row takes an inset amber edge, a 7% tint and a `WAITING` tag.
 * The look lives in `.heph-rail-row` (theme.css); this places the parts.
 *
 * The title is the row's one button, so a keyboard lands on it and Enter opens the
 * row; a click anywhere else on the row opens it too.
 */
export function RailRow(props: RailRowProps): JSX.Element {
  return (
    <div
      class={cx("heph-rail-row", props.class)}
      data-state={
        props.state && props.state !== "idle" ? props.state : undefined
      }
      data-selected={props.selected ? "" : undefined}
      data-unread={props.unread ? "" : undefined}
      data-menu-open={props.menuOpen ? "" : undefined}
      onClick={() => props.onOpen()}
      onContextMenu={(e) => props.onContextMenu?.(e)}
    >
      <Show when={props.fold}>
        {(fold) => (
          <button
            type="button"
            aria-expanded={fold().open}
            aria-label={fold().label}
            class="heph-rail-fold"
            onClick={(e) => {
              e.stopPropagation();
              fold().onToggle();
            }}
          >
            <Icon
              name={fold().open ? "chevron-down" : "chevron-right"}
              size={10}
            />
          </button>
        )}
      </Show>
      <span class="heph-rail-glyph" aria-hidden="true" />
      <button
        type="button"
        aria-current={props.selected ? "true" : undefined}
        class="heph-rail-title min-w-0 truncate py-2 text-left font-sans text-body outline-none focus-visible:underline"
      >
        {props.title}
      </button>
      <span class="heph-rail-readout">
        {props.marks}
        {props.readout}
        <Show when={props.age}>
          <span class="heph-rail-age">{props.age}</span>
        </Show>
        <Show when={props.menu}>
          <span class="heph-rail-more" onClick={(e) => e.stopPropagation()}>
            {props.menu}
          </span>
        </Show>
      </span>
    </div>
  );
}

/** The action tier's tag on a rail row: solid amber, inverse mono. */
export function RailTag(props: { children: JSX.Element }): JSX.Element {
  return <span class="heph-rail-tag">{props.children}</span>;
}
