/**
 * The pieces every viewport surface body is built from.
 *
 * Each of these was written out by hand in every surface that needed it — the scrolling
 * gutter in all of them, the ruled summary line in three, the plate-and-count heading in
 * three, the collapsible card five times — and hand copies drift: one card grew a
 * different body gap, one heading lost its count's `tabular-nums`. One definition each,
 * with the differences that were real kept as props.
 *
 * Chat-specific rather than `~/ui`, because every consumer is a viewport surface and the
 * gutter here is the pane header's, not a general one.
 */

import { Show, type JSX } from "solid-js";
import { Collapse, Text, cx, type IconName } from "~/ui";
import { ProcessRow, Sep } from "./ProcessRow";

/** The frame a surface row sits in: square, one hairline, no fill and no shadow — the
 *  console register's ruled cell (design §6, §7). Contents of a framed region are bare,
 *  so a rounded, lifted card inside the pane's own frame was a container in a
 *  container. */
const SURFACE_CARD = "overflow-hidden border border-line";

/** Surfaces respond to the pane, never the viewport: the operator drags the pane from
 *  320 to 1200px on one screen, so a list goes to two columns where the *pane* has room
 *  for two readable cards (`@3xl`, from the pane frame's container). */
const COLUMNS = "grid grid-cols-1 items-start @3xl:grid-cols-2";

const GAP = { 1: "gap-1", 1.5: "gap-1.5", 2: "gap-2" } as const;

/** A surface's scrolling body, on the pane header's gutter. */
export function SurfaceBody(props: {
  /** Space between the body's blocks. A log of one-line rows packs tighter. */
  gap?: 1 | 2;
  /** Off where the body hands scrolling to a region inside it, so a fixed header
   *  above that region stays put. */
  scroll?: boolean;
  children: JSX.Element;
}): JSX.Element {
  return (
    <div
      class={cx(
        "flex h-full min-h-0 w-full flex-col px-3 pb-2",
        GAP[props.gap ?? 2],
        props.scroll !== false && "overflow-y-auto",
      )}
    >
      {props.children}
    </div>
  );
}

/** The figures a whole surface is read against, on one ruled line that wraps rather
 *  than truncates — every figure on it is one the operator came for. */
export function SurfaceSummary(props: {
  /** `center` where the line leads with a flag, whose box is taller than its text. */
  align?: "baseline" | "center";
  children: JSX.Element;
}): JSX.Element {
  return (
    <div
      class={cx(
        "flex flex-wrap gap-x-2 gap-y-1 border-b border-line pb-2",
        props.align === "center" ? "items-center" : "items-baseline",
      )}
    >
      {props.children}
    </div>
  );
}

/** A plate heading, its count, and the rows it names. */
export function SurfaceSection(props: {
  label: string;
  count?: number;
  /** `bright` where the count is the section's headline figure rather than a tally. */
  countTone?: "dim" | "bright";
  /** More figures on the heading's line, each bringing its own `Sep`. */
  extra?: JSX.Element;
  /** One line under the heading saying what belongs here. */
  hint?: string;
  /** Rows in two columns where the pane is wide. Only for lists that are not a
   *  sequence — a log read out of order is not a log. */
  columns?: boolean;
  /** A rule under the section, where it sits above a different kind of content. */
  ruled?: boolean;
  gap?: 1 | 1.5;
  children?: JSX.Element;
}): JSX.Element {
  const gap = () => GAP[props.gap ?? 1.5];
  return (
    <div
      class={cx(
        "flex flex-col",
        gap(),
        props.ruled && "border-b border-line pb-2",
      )}
    >
      <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1 pt-1">
        <Text variant="plate" tone="dim">
          {props.label}
        </Text>
        <Show when={props.count !== undefined}>
          <Text
            variant="micro"
            tone={props.countTone ?? "dim"}
            class="tabular-nums"
          >
            {props.count}
          </Text>
        </Show>
        {props.extra}
      </div>
      <Show when={props.hint}>
        <Text variant="micro" tone="dim">
          {props.hint}
        </Text>
      </Show>
      <Show when={props.columns} fallback={props.children}>
        <div class={cx(COLUMNS, gap())}>{props.children}</div>
      </Show>
    </div>
  );
}

/**
 * One collapsible row on a surface: a `ProcessRow` on a card, opening onto a body.
 *
 * The open state stays with the caller, because callers rest differently — a failed
 * command opens itself whenever it fails, everything else rests shut — and each of those is a `createAdoptedOpen` call the card should not
 * second-guess.
 */
export function SurfaceCard(props: {
  open: boolean;
  onToggle: () => void;
  icon: IconName;
  iconClass?: string;
  label: string;
  /** The row's accessible name. */
  title: string;
  /** The row's one line of content, truncated beside the label. */
  detail: string;
  trailing?: JSX.Element;
  /** Whether there is anything to reveal. False keeps the toggle but holds the body
   *  shut, so a row with nothing behind it never opens onto an empty band. */
  hasBody?: boolean;
  /** Space between the body's blocks. Output-heavy bodies pack tighter. */
  gap?: 1 | 1.5;
  children: JSX.Element;
}): JSX.Element {
  return (
    <div class={SURFACE_CARD}>
      <ProcessRow
        open={props.open}
        onToggle={props.onToggle}
        icon={props.icon}
        iconClass={props.iconClass ?? "text-dim"}
        label={props.label}
        title={props.title}
        class="hover:bg-raised"
        trailing={props.trailing}
      >
        <Sep />
        <Text variant="micro" tone="default" class="min-w-0 truncate">
          {props.detail}
        </Text>
      </ProcessRow>
      {/* `Collapse`, one animation vocabulary for "a row opening in a panel". */}
      <Collapse open={props.open && props.hasBody !== false}>
        <div
          class={cx(
            "flex flex-col border-t border-line px-2 py-2",
            GAP[props.gap ?? 1.5],
          )}
        >
          {props.children}
        </div>
      </Collapse>
    </div>
  );
}
