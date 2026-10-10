import { Show, createMemo, type JSX } from "solid-js";
import {
  Button,
  Frames,
  Icon,
  MetClock,
  REVEAL_ON_GROUP_HOVER,
  StatusDot,
  Text,
  TypewriterText,
  cx,
  type ContextMenuTriggerProps,
  type Status,
} from "~/ui";
import { REVEAL_SPEED_MS } from "../data";
import type { ChatActivity, ChatOutcome } from "../model";
import { resolveRunState, runStateSpec } from "../runState";

export interface SessionRowProps {
  title: string;
  selected?: boolean;
  pinned?: boolean;
  /** A freshly auto-generated title to type out in place of the static one. The
   *  header owns clearing the reveal; the row just mirrors it while it lasts. */
  reveal?: string;
  /** True while the backend is naming *this* thread — the same throbber the room's
   *  header shows, on the row the retitle was started from. Without it, REGENERATE
   *  TITLE from the rail's menu reports nothing at all until the new name lands. */
  retitling?: boolean;
  /** The backend's status for this thread's live run, when it has one. Colours the
   *  leading edge; absent leaves the row at rest. */
  activity?: ChatActivity;
  /** How the last terminal run ended, when the backend still remembers. Read only at
   *  rest — `activity` is the live truth and both can be set at once. */
  lastOutcome?: ChatOutcome;
  /** The thread's most recent backend write, ISO-8601. Anchors the elapsed clock on a
   *  row whose run is in flight — see the clock's own note for what it can and cannot
   *  claim to measure. */
  updatedAt?: string;
  onOpen: () => void;
  /** Open this row's menu at the cursor. The whole row is the target. */
  onContextMenu: (e: MouseEvent) => void;
  /** ARIA and click wiring for the "···". Spread it rather than rebuilding it, so the
   *  expanded state travels with the handler that changes it. */
  menuTrigger: ContextMenuTriggerProps;
  /** True while *this* row's menu is showing. */
  menuOpen?: boolean;
}

/* The leading edge's colour per state. `idle` and `live` never reach it — only a
   `loud` state colours the edge — but the map is total so the type says so. */
const EDGE: Record<Status, string> = {
  idle: "border-l-transparent",
  live: "border-l-info",
  info: "border-l-info",
  nominal: "border-l-nominal",
  warn: "border-l-warn",
  alert: "border-l-alert",
};

/**
 * A selectable session row with its own actions menu.
 *
 * **Two ways into one menu.** A right-click anywhere on the row opens it at the cursor;
 * the "···" opens the same menu against the button. Right-click is the faster gesture
 * and the one a list invites, but it is invisible and unreachable from a keyboard, so
 * the button is what makes the actions discoverable and operable. Neither alone is the
 * whole control.
 *
 * The row used to carry a pin toggle and a `3D AGO` stamp. Pinning moved into the menu
 * and the stamp into the heading above the run — it answered "when" once per row, in a
 * column the eye had to visit for all of them, to separate threads mostly from the same
 * week. What is left of the pin is a **marker, not a control**: in a sandbox mode the
 * `Pinned` heading already says it, but a code thread is filed under its directory and
 * would otherwise float to the top of its section for no visible reason.
 *
 * The label and the "···" are sibling buttons (not nested) so neither swallows the
 * other's click.
 *
 * A thread whose run is live colours its leading edge. Every row reserves the rule
 * (transparent at rest) so marking one can't shift the list.
 *
 * **A finished run is no longer indistinguishable from a thread that never ran.** The
 * row reports two backend facts through one table (`runState.ts`), and it reports them
 * with two different devices on purpose:
 *
 * - **The edge is for what needs you.** Live work, and the two endings that mean the
 *   operator is wanted — a failure and a limit — colour it. Colouring it for every
 *   `done` was the obvious version and the wrong one: §5 rule 1 is that a screen at
 *   rest is grayscale, and a rail where thirty finished threads are each edged green is
 *   a rail nobody scans for the marked one. That is the whole mechanism, spent.
 * - **The dot is for what happened.** Every terminal outcome gets a 6px `StatusDot` at
 *   rest, which is exactly the trade `StatusFlag` documents: confining the hue to a
 *   mark keeps the state legible from across the room and keeps the accent budget
 *   intact. It shows only when nothing is live, since a live run is already speaking.
 *
 * Self-limiting by construction, which is what makes it bearable at all: the backend's
 * run registry is bounded, so only recently-active threads carry an outcome and the
 * older rail stays grey without the interface having to ration it.
 */
export function SessionRow(props: SessionRowProps): JSX.Element {
  /** The one state this row is in — live if there is one, else how it ended. */
  const state = createMemo(() =>
    resolveRunState(props.activity, props.lastOutcome),
  );
  const spec = createMemo(() => {
    const s = state();
    return s ? runStateSpec(s) : undefined;
  });
  const edge = (): string => {
    const s = spec();
    return EDGE[s?.loud ? s.status : "idle"];
  };

  return (
    <div
      // On the row's own element, not the label button: the target is the whole row,
      // including the strip the "···" sits on and the padding around it. A right-click
      // that lands two pixels off the label and does nothing reads as the menu being
      // broken rather than as having missed.
      onContextMenu={(e) => props.onContextMenu(e)}
      class={cx(
        // No rule between rows (§7) — the hover fill and the rhythm are what
        // make this read as a list, and the leading edge shows only when there
        // is something to report.
        "group flex items-center overflow-hidden rounded-ctl border-l transition-colors hover:bg-raised",
        edge(),
        props.selected && "bg-raised",
      )}
    >
      <button
        type="button"
        onClick={() => props.onOpen()}
        class="flex min-w-0 flex-1 items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <Show
          when={props.reveal}
          fallback={
            <Text
              variant="label"
              tone={props.selected ? "bright" : "default"}
              class="truncate"
            >
              {props.title}
            </Text>
          }
        >
          {(reveal) => (
            <TypewriterText
              variant="label"
              tone={props.selected ? "bright" : "default"}
              text={reveal()}
              speed={REVEAL_SPEED_MS}
              class="truncate"
            />
          )}
        </Show>
        <Show when={props.retitling}>
          <Frames class="shrink-0 text-info" />
          <span class="sr-only">naming this thread</span>
        </Show>
        {/* How long this thread has been working, on the row rather than only in the
            room — the rail is where the operator watches several at once, and until
            now a run three threads down reported that it was happening and never how
            long it had been happening.

            **It is anchored on `updated_at`, and that is an approximation the backend
            cannot yet improve on.** The list DTO carries no run start; the thread's
            last write is the turn that started the run, which is within a second of it
            on a fresh send and drifts on a thread the run has since written to. So it
            is shown only while work is live, where the figure is still the answer to
            "how long has this been going", and it is never shown at rest, where the
            same number would quietly become the age of the record. Still a backend
            timestamp formatted, never a clock the frontend keeps.

            No `T+` prefix: at `micro` in a 248px rail the two characters cost more
            than they explain, and a ticking mono figure beside a live edge is already
            reading as elapsed. */}
        <Show when={spec()?.live && props.updatedAt}>
          {(startedAt) => (
            <MetClock
              startedAt={startedAt()}
              variant="micro"
              prefix={false}
              class="shrink-0"
            />
          )}
        </Show>
        <Show when={spec()}>
          {(s) => <span class="sr-only">{s().spoken}</span>}
        </Show>
      </button>
      {/* How the last run ended, at rest. Hidden while something is live — the edge and
          the clock are already saying what this thread is doing, and a second mark
          reporting an older fact beside them reads as a contradiction. */}
      <Show when={!spec()?.live && spec()}>
        {(s) => <StatusDot status={s().status} class="mr-1.5 shrink-0" />}
      </Show>
      {/* Marker, not a control — inside the row's own gutter so it cannot be
          mistaken for the button beside it. `sr-only` text because the glyph is
          the only thing carrying the state (§12). */}
      <Show when={props.pinned}>
        <Icon name="pin" size={12} class="shrink-0 text-dim" />
        <span class="sr-only">pinned</span>
      </Show>
      <Button
        {...props.menuTrigger}
        variant="ghost"
        size="sm"
        aria-label="Thread actions"
        // Held visible while this row's own menu is open. `REVEAL_ON_GROUP_HOVER`
        // covers hover, focus and touch, but the panel is portalled to the body — so
        // the moment it opens, focus leaves the row, `focus-within` stops holding, and
        // the trigger fades out from under the menu it just opened.
        class={cx("shrink-0", !props.menuOpen && REVEAL_ON_GROUP_HOVER)}
      >
        ···
      </Button>
    </div>
  );
}
