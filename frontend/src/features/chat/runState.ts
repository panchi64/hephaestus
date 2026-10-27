/**
 * What state a thread's run is in, and how the interface says it — once, for every
 * surface that asks.
 *
 * **Why this is a module and not a table inside `SessionRow`.** Three surfaces report
 * the same fact: the rail's row marks its edge, the composer's header line names it, and
 * a cold thread's recap prints it in a cell. A table per surface is three tables that
 * agree until the day one of them gains a state, and the failure is silent — the rail
 * calls a run `BLOCKED` in amber while the room three inches away still calls it
 * finished. So the mapping is derived once and passed down, per the repo's own rule.
 *
 * **The frontend decides nothing here.** Both inputs are backend-derived — `activity`
 * from the live run registry, `lastOutcome` from the last terminal run in it — and this
 * file only chooses words and a tone for them. The one rule it does encode is the
 * *precedence* the contract states: while a run is live, that is the thread's state; the
 * outcome is what it falls back to at rest.
 */

import { ACTION_TONES, type ActionTone, type Status } from "~/ui";
import type { ChatActivity, ChatOutcome } from "./model";

/** Either half of the pair, resolved to the one state a surface renders. */
export type RunState = ChatActivity | ChatOutcome;

export interface RunStateSpec {
  /** The machine voice (§2) — what a mono readout prints. Uppercase by construction:
   *  `meta`/`plate` uppercase anyway, and writing it out keeps the two in agreement. */
  readout: string;
  /** The same fact as a screen reader should hear it, in the interface's own voice.
   *  State carried by a coloured edge alone is state an operator can't read (§12), so
   *  every surface that lights something also says this. */
  spoken: string;
  /** The accent the state is reported in — `StatusDot`/`StatusFlag`'s vocabulary, and
   *  the colour of the rail row's edge when the state is `loud`. */
  status: Status;
  /** Work is still in flight — the clock should tick and the dot should pulse. */
  live: boolean;
  /**
   * Worth marking a whole row's edge from across the room.
   *
   * **This is the accent budget, written down.** §5 rule 1 says a screen at rest is
   * grayscale, and a rail that marks green on every thread that ever finished reports
   * nothing at all — the eye stops finding the marked one, which is the entire mechanism
   * (§10.15). So the edge is coloured for live work and for the two endings that mean
   * the operator is needed; a plain `done` and a `cancelled` get only the dot.
   */
  loud: boolean;
}

const SPECS = {
  queued: {
    readout: "QUEUED",
    spoken: "queued",
    status: "queue",
    live: true,
    loud: true,
  },
  running: {
    readout: "RUNNING",
    spoken: "running",
    status: "info",
    live: true,
    loud: true,
  },
  awaiting_input: {
    readout: ACTION_TONES.approval.readout,
    spoken: ACTION_TONES.approval.spoken,
    status: ACTION_TONES.approval.status,
    live: true,
    loud: true,
  },
  done: {
    readout: "DONE",
    spoken: "finished",
    status: "nominal",
    live: false,
    loud: false,
  },
  error: {
    // FAILED, not ERROR: the operator asks "did it work", and the answer to that is a
    // verb. `error` is the backend's word for the record it kept.
    readout: "FAILED",
    spoken: "ended in an error",
    status: "alert",
    live: false,
    loud: true,
  },
  blocked: {
    readout: "BLOCKED",
    spoken: "stopped at a limit",
    status: "warn",
    live: false,
    loud: true,
  },
  cancelled: {
    // An idle dot, because nothing went wrong and nobody needs to act: the operator
    // stopped it, and they already know.
    readout: "CANCELLED",
    spoken: "cancelled",
    status: "idle",
    live: false,
    loud: false,
  },
} satisfies Record<RunState, RunStateSpec>;

/** Total over the union by construction — there is no unknown arm to hide a missing
 *  row behind, because a value that isn't one of the seven never had this type.
 *  A run parked on the operator reports in its ask's tone; no kind reads as an approval. */
export function runStateSpec(
  state: RunState,
  awaiting?: ActionTone,
): RunStateSpec {
  const spec = SPECS[state];
  if (state !== "awaiting_input" || !awaiting) return spec;
  const tone = ACTION_TONES[awaiting];
  return {
    ...spec,
    readout: tone.readout,
    spoken: tone.spoken,
    status: tone.status,
  };
}

/**
 * The one state to render, out of the two fields the backend sends.
 *
 * **`activity` wins, and the contract says both can be set at once** — a thread that
 * finished a minute ago and has since been sent to again carries a terminal outcome
 * *and* a running run. Reporting the outcome there would tell the operator their thread
 * is finished while it is visibly working.
 *
 * Returns `undefined` for a thread the backend can say nothing about, which is the
 * ordinary case and not an absence to paper over: the run registry is bounded, so a
 * restart leaves every thread here.
 */
export function resolveRunState(
  activity: ChatActivity | undefined,
  lastOutcome: ChatOutcome | undefined,
): RunState | undefined {
  return activity ?? lastOutcome;
}
