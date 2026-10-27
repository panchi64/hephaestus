import { Match, Show, Switch, createMemo, type JSX } from "solid-js";
import { MetClock, type ActionTone, type Status } from "~/ui";
import type { ChatActivity, ChatOutcome } from "../model";
import type { RunClock, RunQueue } from "../stream/fold";
import { resolveRunState, runStateSpec } from "../runState";

export interface ComposerHeaderStatusProps {
  /** The thread's name, or null for one not yet created. */
  title: () => string | null;
  /** The room's stream is driving a run — a turn, or a hand-started fold. */
  streaming: () => boolean;
  /** That run is a fold rather than a turn. */
  compacting: () => boolean;
  /** The live run's transport gave up reconnecting. */
  detached: () => boolean;
  /** The run's own start instant and newest step, as the stream reported them. */
  runClock: () => RunClock | null;
  /** Where the run waits for its model endpoint, when it is waiting. */
  runQueue: () => RunQueue | null;
  /** The backend's status for this thread's live run, when it has one. */
  activity: () => ChatActivity | undefined;
  /** What an `awaiting_input` run is parked on. */
  awaitingKind: () => ActionTone | undefined;
  /** How this thread's last terminal run ended, when the backend still remembers. */
  lastOutcome: () => ChatOutcome | undefined;
}

/* One Status → text colour, as the plate's pieces carry their own tone. The plate is
   mono already, so a `Text` here would restate a size it cannot change. */
const STATUS_CLASS: Record<Status, string> = {
  idle: "",
  live: "text-nominal",
  nominal: "text-nominal",
  warn: "text-warn",
  alert: "text-alert",
  info: "text-info",
  queue: "text-queue",
};

/**
 * The left of the composer's plate: what the thread is doing, said where the operator
 * is about to type. At rest it names the thread — `New thread · ready` before one
 * exists — so the unit says which conversation the next message goes into.
 *
 * **Precedence, highest first**, and each rung says why it outranks the next:
 * - `Disconnected` — a run whose transport gave up is still guarded as in flight, so the
 *   plate would otherwise go on claiming a run nothing is listening to.
 * - A run waiting in line for its model endpoint: `waiting for <endpoint> · #<place>`,
 *   until `run.started` ends the wait.
 * - The live run: `Streaming` (or `Folding` for a hand-started fold, which is a run of
 *   its own) and the step it is on, once the stream has reported one. The plate turns
 *   info with a blinking square around it; the clock is the plate's aside.
 * - The resolved run state from the same `runState.ts` table the rail reads — except
 *   `done`, which is the ordinary ending and says nothing `ready` does not. So a
 *   failed, blocked or cancelled run, and one the backend reports live that this room
 *   is not driving, are named in their own tone beside the thread's name.
 */
export function ComposerHeaderStatus(
  props: ComposerHeaderStatusProps,
): JSX.Element {
  const atRest = createMemo(() => {
    const s = resolveRunState(props.activity(), props.lastOutcome());
    return s && s !== "done"
      ? runStateSpec(s, props.awaitingKind())
      : undefined;
  });
  const name = () => props.title() ?? "New thread";

  return (
    <Switch fallback={`${name()} · ready`}>
      <Match when={props.detached()}>
        <span class="text-alert">Disconnected</span>
      </Match>
      <Match when={props.streaming() && props.runQueue()}>
        {(queue) => `waiting for ${queue().endpoint} · #${queue().position}`}
      </Match>
      <Match when={props.streaming()}>
        {props.compacting() ? "Folding" : "Streaming"}
        <Show when={props.runClock()?.step}>
          {(step) => ` · step ${step()}`}
        </Show>
      </Match>
      <Match when={atRest()}>
        {(s) => (
          <>
            {name()} ·{" "}
            <span class={STATUS_CLASS[s().status]}>{s().readout}</span>
          </>
        )}
      </Match>
    </Switch>
  );
}

export interface ComposerHeaderAsideProps {
  streaming: () => boolean;
  runClock: () => RunClock | null;
  /** Messages waiting in the conversation's inbox. */
  queued: () => number;
  /** Whether the dock listing them is open. */
  queuedOpen: () => boolean;
  onToggleQueued: () => void;
  /** A worktree thread's branch, when it has one. */
  branch: () => string | undefined;
}

/**
 * The right of the composer's plate: the run's clock while one streams (from the
 * backend's own `run.started` instant — never estimated), queued messages waiting on
 * the operator's say-so, and otherwise the branch a worktree thread writes to.
 */
export function ComposerHeaderAside(
  props: ComposerHeaderAsideProps,
): JSX.Element {
  return (
    <Switch>
      <Match when={props.queued()}>
        {(count) => (
          <button
            type="button"
            class={`uppercase hover:text-bright ${props.queuedOpen() ? "text-bright" : "text-warn"}`}
            aria-expanded={props.queuedOpen()}
            aria-label={`${count()} queued, ${props.queuedOpen() ? "hide" : "show"}`}
            onClick={props.onToggleQueued}
          >
            {count()} queued
          </button>
        )}
      </Match>
      <Match when={props.streaming() && props.runClock()}>
        {(clock) => (
          <MetClock
            variant="meta"
            startedAt={clock().startedAt}
            prefix={false}
          />
        )}
      </Match>
      <Match when={!props.streaming() && props.branch()}>
        {(branch) => <span class="normal-case">{branch()}</span>}
      </Match>
    </Switch>
  );
}
