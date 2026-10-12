import { Show, children, createSignal, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { Collapse } from "./Collapse";

/** Which glyph says "there is more under here".
 *
 *  A chevron reads as *direction* — fine on a heading that is only a heading, wrong
 *  beside anything that could be navigated to, where it reads as "go there". The
 *  registration-crosshair plus/minus can't be misread, so it is what the rail's area
 *  headers use — their label is a link.
 *
 *  The thread list's workspace headings take the **chevron** instead, and for the
 *  mirror-image reason: nothing there navigates, and the row now carries a real `+`
 *  meaning *start a thread in this directory*. A plus/minus marker an inch from a plus
 *  button is the one confusion this glyph choice exists to prevent. */
export type DisclosureMarker = "chevron" | "plusminus";

/** The glyph beside a label. A plus needs a touch more than a chevron to read as a
 *  plus at all, hence the two sizes. */
const MARKER_SIZE: Record<DisclosureMarker, number> = {
  chevron: 12,
  plusminus: 14,
};

/** A lone toggle is the control rather than an ornament on one, so it gets the
 *  larger glyph and a target to go with it. */
const TOGGLE_SIZE = 16;

function markerName(
  marker: DisclosureMarker,
  open: boolean,
): "chevron-down" | "chevron-right" | "minus" | "plus" {
  if (marker === "plusminus") return open ? "minus" : "plus";
  return open ? "chevron-down" : "chevron-right";
}

export interface DisclosureProps {
  /** Label shown beside the marker. It names a region, so it is set as a plate
   *  (mono uppercase, engraved tracking) — and stays the accessible name. */
  label: string;
  /** Controlled open state. Omit to let the component own it. */
  open?: boolean;
  /** Toggle handler. Required when `open` is supplied. */
  onToggle?: () => void;
  /** Initial state when uncontrolled. Defaults to closed. */
  defaultOpen?: boolean;
  /** Which glyph leads the trigger. Defaults to the chevron. */
  marker?: DisclosureMarker;
  /** Rendered in place of the default `<Text>` label. `label` stays required — it
   *  remains the accessible name — so this is for a label that has *parts*: the thread
   *  list's headings read `parent/name` and must truncate at the head, since clipping
   *  the tail would cut the directory name, which is the half that identifies the
   *  section. */
  labelNode?: JSX.Element;
  /** Rendered inside the trigger after the label — a peek line, a count, a status
   *  glyph. */
  trailing?: JSX.Element;
  /** Controls rendered as the trigger's **sibling**, on the same row.
   *
   *  Not `trailing`: that renders inside the trigger, and a button inside a button is
   *  invalid HTML — the constraint that forced `AreaSection` to hand-roll its header out
   *  of `DisclosureToggle` and a link. Covering the case here is what keeps the thread
   *  list from becoming a second such exception. The row becomes a `group`, so an action
   *  can reveal on hover (`REVEAL_ON_GROUP_HOVER`) without naming a group of its own. */
  actions?: JSX.Element;
  /** Extra classes for the **whole header row** — the trigger and its actions together.
   *  Where a fill belongs when it is marking the section rather than the toggle: painted
   *  on `triggerClass` instead, it stops short of the controls and the row reads as
   *  half-highlighted. */
  rowClass?: string;
  /** Extra classes for the trigger row (e.g. `w-full` for a full-width header). */
  triggerClass?: string;
  /** Body wrapper classes. Replaces the default spacing rather than adding to it,
   *  so a caller can set its own margin without two competing utilities. */
  class?: string;
  children: JSX.Element;
}

/**
 * A labelled show/hide section — marker, label, collapsing body.
 *
 * The look is the one the chat surface's own collapsibles converged on: a small glyph
 * that flips on open, a dim label that brightens on hover, and the body beneath. Works
 * controlled (pass `open` + `onToggle`) or uncontrolled.
 *
 * **The body opens over its own height** (`Collapse`) rather than appearing outright.
 * Content that simply vanishes takes everything below it with it, which is the single
 * most common reason a screen reads as having redrawn — and a disclosure is the exact
 * case that rule is written for.
 *
 * The plus/minus `marker` is what lets the rail's area headers and the thread list's
 * workspace headings live here instead of hand-rolling a header each. Both used to, and
 * the copies had already drifted apart on the accessible name and on which parts of the
 * row take the hover fill.
 *
 * One shape still can't be this component: the nav rail's header is a *link* with the
 * toggle beside it (a button inside a link is invalid HTML, the same reason `ListRow`'s
 * `right` slot is a span). `DisclosureToggle` is that control on its own, so even the
 * exception shares the aria and the paint.
 *
 * `ToolCallCard` and its siblings also stay off this: their trigger row carries a whole
 * cluster of controls, and covering that would mean prop-configuring arbitrary trigger
 * children, at which point the component stops being simpler than the markup it hides.
 * They share `ProcessRow` instead.
 */
export function Disclosure(props: DisclosureProps): JSX.Element {
  const [uncontrolled, setUncontrolled] = createSignal(
    props.defaultOpen ?? false,
  );
  const isOpen = (): boolean => props.open ?? uncontrolled();
  const marker = (): DisclosureMarker => props.marker ?? "chevron";
  const toggle = (): void => {
    if (props.open === undefined) setUncontrolled((v) => !v);
    props.onToggle?.();
  };
  // Resolved once. A JSX prop compiles to a getter, so testing `props.labelNode` and
  // then rendering it would build the node twice over and discard one — and this header
  // re-renders on the rail's three-second activity poll.
  const label = children(() => props.labelNode);

  return (
    <div>
      {/* One row, always — the trigger alone is just a row of one. Branching the
          wrapper instead would mean building the trigger twice over so both arms
          could hold one, and this header re-renders on every list refresh. */}
      <div class={cx("group flex items-center", props.rowClass)}>
        <button
          type="button"
          aria-expanded={isOpen()}
          onClick={(e) => {
            // Trigger clicks stop here: a disclosure nested inside its own clickable
            // wrapper would otherwise toggle twice and appear inert.
            e.stopPropagation();
            toggle();
          }}
          class={cx(
            "flex items-center gap-1 text-left text-dim transition-colors hover:text-text",
            // With actions beside it the trigger takes the row's slack and nothing
            // more, so the controls keep their width and the label is what gives way.
            props.actions ? "min-w-0 flex-1" : undefined,
            props.triggerClass,
          )}
        >
          <Icon
            name={markerName(marker(), isOpen())}
            size={MARKER_SIZE[marker()]}
            class="shrink-0"
          />
          {/* Truncating, always. A section's name is whatever it is named — a
              workspace heading is a directory path — and a header that cannot
              overflow its own trigger is the only version of this that is safe to
              hand an arbitrary label. */}
          <Show
            when={label()}
            fallback={
              <Text variant="plate" tone="dim" class="min-w-0 truncate">
                {props.label}
              </Text>
            }
          >
            {label()}
          </Show>
          {props.trailing}
        </button>
        {props.actions}
      </div>
      <Collapse open={isOpen()}>
        <div class={props.class ?? "mt-2"}>{props.children}</div>
      </Collapse>
    </div>
  );
}

export interface DisclosureToggleProps {
  open: boolean;
  onToggle: () => void;
  /** What the section is called — the toggle carries no visible text, so this is
   *  the whole of its accessible name ("Expand Sources"). */
  label: string;
  class?: string;
}

/** The plus/minus control by itself, for the one header that cannot be a single
 *  button (see `Disclosure`). It owns the accessible name and the hover paint so the
 *  exception cannot drift from the rule. */
export function DisclosureToggle(props: DisclosureToggleProps): JSX.Element {
  return (
    <button
      type="button"
      aria-expanded={props.open}
      aria-label={`${props.open ? "Collapse" : "Expand"} ${props.label}`}
      onClick={props.onToggle}
      class={cx(
        "flex size-7 shrink-0 items-center justify-center text-dim transition-colors hover:bg-raised hover:text-text",
        props.class,
      )}
    >
      <Icon name={markerName("plusminus", props.open)} size={TOGGLE_SIZE} />
    </button>
  );
}
