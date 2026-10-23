import { Show, createMemo, type JSX } from "solid-js";
import {
  Frames,
  Icon,
  MetClock,
  RailRow,
  RailTag,
  TypewriterText,
  type ContextMenuTriggerProps,
  type RailRowProps,
  type RailRowState,
} from "~/ui";
import { REVEAL_SPEED_MS } from "../data";
import type { ChatActivity, ChatOutcome } from "../model";
import { resolveRunState, runStateSpec, type RunState } from "../runState";
import { rowAge } from "../sessionGroups";

export interface SessionRowProps {
  title: string;
  selected?: boolean;
  pinned?: boolean;
  /** Something about this thread the operator has not seen yet — the backend's unread
   *  notifications for it. */
  unread?: boolean;
  reveal?: string;
  retitling?: boolean;
  activity?: ChatActivity;
  lastOutcome?: ChatOutcome;
  updatedAt?: string;
  onOpen: () => void;
  onContextMenu: (e: MouseEvent) => void;
  menuTrigger: ContextMenuTriggerProps;
  menuOpen?: boolean;
  /** The chevron folding the agent threads opened under this one. */
  fold?: RailRowProps["fold"];
}

/** A run state as what the row's glyph column says. Only the states worth marking
 *  from across the room get one: live work, a wait on the operator, a failure or a
 *  limit. Everything else is the idle grey square. */
const ROW_STATE: Record<RunState, RailRowState> = {
  queued: "queued",
  running: "running",
  awaiting_input: "waiting",
  done: "idle",
  error: "failed",
  blocked: "failed",
  cancelled: "idle",
};

/** What a failed row says in its readout, in the alert tone. */
const FAILURE_WORD: Partial<Record<RunState, string>> = {
  error: "failed",
  blocked: "limit",
};

/**
 * One thread in the rail: a square glyph, the title, and a readout (`RailRow`).
 *
 * - **idle** — a grey square and the thread's age (only inside 30 days);
 * - **unread** — a bright square and a bright title;
 * - **queued** — the queue square, still, and `queued` in the queue tone;
 * - **running** — the accent square blinking and the elapsed clock;
 * - **waiting** — the action tier's echo: an amber inset edge, a tint, `WAITING`;
 * - **failed / limit** — a red square and the word in red.
 *
 * Under the pointer the age gives way to the row's `···` menu, in the same slot.
 */
export function SessionRow(props: SessionRowProps): JSX.Element {
  const state = createMemo(() =>
    resolveRunState(props.activity, props.lastOutcome),
  );
  const spec = createMemo(() => {
    const s = state();
    return s ? runStateSpec(s) : undefined;
  });
  const rowState = (): RailRowState => {
    const s = state();
    return s ? ROW_STATE[s] : "idle";
  };
  const failure = () => {
    const s = state();
    return s ? FAILURE_WORD[s] : undefined;
  };

  const title = (
    <Show
      when={props.reveal}
      fallback={<span class="truncate">{props.title}</span>}
    >
      {(reveal) => (
        <TypewriterText
          variant="body"
          tone={props.selected ? "bright" : "default"}
          text={reveal()}
          speed={REVEAL_SPEED_MS}
          class="truncate"
        />
      )}
    </Show>
  );

  // How long this thread has been working, on the row — the rail is where the operator
  // watches several at once. The list carries no run start; the thread's last write is
  // the turn that started the run, so it is shown only while work is live, where it is
  // still the answer to "how long has this been going", and never at rest.
  const readout = (): JSX.Element => (
    <>
      <Show when={props.retitling}>
        <Frames class="shrink-0 text-info" />
        <span class="sr-only">naming this thread</span>
      </Show>
      <Show when={rowState() === "waiting"}>
        <RailTag>Waiting</RailTag>
      </Show>
      <Show when={rowState() === "queued"}>
        <span>queued</span>
      </Show>
      <Show when={rowState() === "running" && props.updatedAt}>
        {(startedAt) => (
          <MetClock startedAt={startedAt()} variant="micro" prefix={false} />
        )}
      </Show>
      <Show when={failure()}>{(word) => <span>{word()}</span>}</Show>
      <Show when={spec()}>
        {(s) => <span class="sr-only">{s().spoken}</span>}
      </Show>
    </>
  );

  // The age only where nothing louder holds the slot.
  const age = () =>
    rowState() === "idle" && props.updatedAt && !props.retitling
      ? rowAge(props.updatedAt)
      : undefined;

  return (
    <RailRow
      state={rowState()}
      selected={props.selected}
      unread={props.unread && !props.selected}
      title={title}
      readout={readout()}
      age={age()}
      marks={
        <Show when={props.pinned}>
          <Icon name="pin" size={12} class="shrink-0" />
          <span class="sr-only">pinned</span>
        </Show>
      }
      menu={
        <button
          {...props.menuTrigger}
          type="button"
          aria-label="Thread actions"
          class="px-0.5 text-dim hover:text-bright"
        >
          ···
        </button>
      }
      menuOpen={props.menuOpen}
      onOpen={props.onOpen}
      onContextMenu={props.onContextMenu}
      fold={props.fold}
    />
  );
}
