import {
  Show,
  children,
  createContext,
  createEffect,
  createSignal,
  useContext,
  type Accessor,
  type JSX,
  type Setter,
} from "solid-js";
import { Dynamic } from "solid-js/web";
import { Icon, Text, cx, type IconName } from "~/ui";
import type { Frame } from "../frames";
import { useArrival } from "./arrival";
import { Socket } from "./Socket";

export interface AdoptedOpen {
  open: Accessor<boolean>;
  setOpen: Setter<boolean>;
  /** The one thing every caller does with the setter. */
  toggle: () => void;
}

/**
 * A disclosure that is its own, until the turn says otherwise.
 *
 * Every collapsible card in a turn owns its open state *and* yields to the turn's
 * expand-all / collapse-all — with local toggles working freely between two of those
 * presses. Spelled out that is a signal plus an effect, and it was spelled out in five
 * cards: the reasoning trace, the tool call, the host terminal, the injected context and
 * the review row. Five copies of a rule is five chances for one of them to adopt
 * `undefined` and snap shut, or to stop adopting at all.
 *
 * `initial` is the card's own resting state, read once at creation: a failed tool call
 * opens itself so the reason is on screen, a host terminal shows its output, and
 * everything else rests closed.
 */
export function createAdoptedOpen(
  props: { open?: boolean },
  initial = false,
): AdoptedOpen {
  const [open, setOpen] = createSignal(initial);
  // `undefined` is "nobody is driving this" — the card keeps whatever it has, which is
  // what lets a local toggle survive between two expand-all presses.
  createEffect(() => {
    if (props.open !== undefined) setOpen(props.open);
  });
  return { open, setOpen, toggle: () => setOpen((v) => !v) };
}

/** Whether rows are drawn as a schematic — the work log's voice (§8, §9).
 *
 *  Provided by the work log around its rows, so every kind of row it holds (a
 *  tool call, reasoning, a review, injected context) speaks it without each
 *  card threading a prop through. In it the glyph sits in a framed `Socket`
 *  that a `Branch` elbow lands on, the label is the machine's `meta` at the dim
 *  tone, and the chevron moves to the row's far end. Everywhere else — the
 *  log's own header, the panel surfaces — rows keep the ordinary voice. */
export const SchematicContext = createContext(false);

/** The `·` between segments of one row. Quiet enough to read as punctuation
 *  rather than as another value. */
export function Sep(): JSX.Element {
  return (
    <Text variant="micro" tone="dim" class="shrink-0 opacity-50 select-none">
      ·
    </Text>
  );
}

/** One row of the agent's process: `chevron · glyph · Label · …` with an optional
 *  cluster pinned right — or, inside a work log (`SchematicContext`),
 *  `[glyph] LABEL · … chevron`, with the chevron moved to make room for the elbow.
 *
 *  Every kind of work in a turn renders through this — a tool call, a settled
 *  reasoning trace, the work log's own header — because they sit in one column on
 *  one trunk and a column only reads as a sequence if its rows share an anatomy.
 *  They used to be three separate idioms: the tool row had a glyph, a detail and
 *  a right cluster; the reasoning accordion had a bare chevron and a word; the
 *  work log header had a shouted uppercase string. Three species in one column is
 *  why the turn read as a log rather than as the agent narrating its work.
 *
 *  The truncation rules here are the load-bearing part and are easy to lose.
 *
 *  Every segment carries `truncate`, because `overflow:hidden` is what lets a flex
 *  item shrink below its content at all — without it the row does not truncate, it
 *  overflows, and a `shrink-0` segment inside this `min-w-0 flex-1` button escapes
 *  the button's box and paints over the cluster to its right.
 *
 *  **The label is the exception, and it is bounded rather than pinned.** Leaving it
 *  to plain flex was the old rule, on the theory that flex shrinks the longest
 *  segment hardest so the shortest survives. It does not survive: at 375px a row
 *  read `R… · backend/ap… · 412 li…`, and the *label* is the one segment that must
 *  not go — glyph plus label is how a column of rows is parsed before a word of
 *  detail is read. So it takes `shrink-0` to opt out of the squeeze, and
 *  `max-w-2/5` so a long label ("Host command", a humanized `external_*` name)
 *  still cannot run past its share and out of the box. The detail beside it absorbs
 *  the loss instead, which is the right place for it: a truncated path is still
 *  recognizable, a truncated verb is not.
 *
 *  **The trailing cluster is capped at half the row and wraps inside that half.** It
 *  used to be `shrink-0`, so in a narrow pane three flags took the whole row and the
 *  detail — the command, the topic — truncated to nothing. The flags wrap onto a second
 *  line instead, and the half they leave is the floor the label and detail read in. */
export function ProcessRow(props: {
  open: boolean;
  /** Whether there is anything behind the chevron. Defaults to true.
   *
   *  A row with nothing to reveal keeps its whole anatomy — the column reads as a
   *  sequence because every row is the same shape — but drops the chevron and the
   *  button, since a control that opens onto nothing is worse than no control. */
  foldable?: boolean;
  onToggle: () => void;
  /** The family glyph. Omitted only where the row has no kind to name. */
  icon?: IconName;
  /** Tone class for the glyph — how a row carries its state without a word. */
  iconClass?: string;
  /** The glyph's outline in a schematic: what kind of thing the row is. */
  frame?: Frame;
  /** Dashes the outline: a row the chassis put there rather than the model. */
  dashed?: boolean;
  /** The row's name, in the interface's voice: "Read", "Reasoning", "Work log". */
  label: string;
  /** Everything between the label and the right cluster. Segments supply their
   *  own `Sep`, since only the caller knows which of them are present. */
  children?: JSX.Element;
  /** Pinned right — elapsed time, a copy button, an alert flag. */
  trailing?: JSX.Element;
  /** Full accessible name for the trigger, when the label alone is ambiguous
   *  (a tool row's label is the short human one, not the registry name). */
  title?: string;
  class?: string;
}): JSX.Element {
  const trailing = children(() => props.trailing);
  // Read once: a row does not move between a work log and a card.
  const schematic = useContext(SchematicContext);
  const arrival = useArrival();
  const chevron = (): JSX.Element => (
    <Icon
      name={props.open ? "chevron-down" : "chevron-right"}
      size={12}
      class="text-dim"
    />
  );
  return (
    <div
      /* No hover fill here by default: a row that sits on a card wants one, and a
         row that is ambient chrome on the page does not (§10.2 — a card is a
         claim on attention, and the work log's own header is the least
         attention-worthy thing in a turn). Callers that own a surface pass
         `hover:bg-raised` themselves. */
      class={cx(
        "flex w-full items-center justify-between gap-2 pr-1.5 transition-colors",
        arrival.body,
        props.class,
      )}
    >
      <Dynamic
        component={props.foldable === false ? "div" : "button"}
        ref={arrival.sweep}
        type={props.foldable === false ? undefined : "button"}
        aria-expanded={props.foldable === false ? undefined : props.open}
        onClick={
          props.foldable === false
            ? undefined
            : (e: MouseEvent) => {
                // A row nested inside its own clickable wrapper would otherwise
                // toggle twice and appear inert.
                e.stopPropagation();
                props.onToggle();
              }
        }
        class={cx(
          /* `py-1`: a 24px row, the composer's status-cell height. The geometry
             that meets this row — `Branch`'s elbow, `WorkLog`'s trunk stub — is
             derived from it and moves with it. */
          "flex min-w-0 flex-1 items-center gap-2 py-1 text-left",
          /* Flush left in a schematic, so the branch's elbow lands on the socket. */
          schematic ? "pr-2" : "px-2",
        )}
        title={props.title}
      >
        {/* The chevron's slot is held open even with nothing to reveal, so the
            glyph and label of every row in the column still line up. A schematic
            row has no slot: the elbow is what leads into it. */}
        <Show when={!schematic}>
          <Show
            when={props.foldable !== false}
            fallback={<span class="w-3 shrink-0" aria-hidden="true" />}
          >
            {chevron()}
          </Show>
        </Show>
        <Show when={props.icon}>
          {(name) =>
            schematic ? (
              <Socket
                icon={name()}
                frame={props.frame}
                dashed={props.dashed}
                lead
                iconClass={props.iconClass}
                charge={arrival.socket}
              />
            ) : (
              <Icon
                name={name()}
                size={12}
                class={cx(props.iconClass ?? "text-dim", arrival.socket)}
              />
            )
          }
        </Show>
        <Text
          variant={schematic ? "meta" : "label"}
          /* A schematic label is telemetry, so it recedes to the ambient tone:
             the sentence it serves (the work log's reason, a call's narration) is
             what should be read first, and a bright uppercase word would win that. */
          tone={schematic ? "dim" : "bright"}
          class="max-w-2/5 shrink-0 truncate"
        >
          {props.label}
        </Text>
        {props.children}
        {/* The disclosure moves to the row's end: the leading edge is the elbow's. */}
        <Show when={schematic && props.foldable !== false}>
          <span class="ml-auto flex shrink-0">{chevron()}</span>
        </Show>
      </Dynamic>
      {/* `children()` and not `props.trailing` read twice. Solid props are
          getters, so reading one in `Show`'s condition AND again as the span's
          child builds the whole cluster twice and throws the first copy away —
          and because the condition is a memo that tracks whatever the cluster
          read while being built, it does it again on every `status`/`elapsedMs`
          change. This resolves it once and hands the same nodes to both. */}
      <Show when={trailing()}>
        <span class="flex max-w-1/2 min-w-0 flex-wrap items-center justify-end gap-x-2 gap-y-0.5">
          {trailing()}
        </span>
      </Show>
    </div>
  );
}
