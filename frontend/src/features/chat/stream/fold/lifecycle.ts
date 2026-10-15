/**
 * The run's own lifecycle: it started, took a step, reported its metrics, hit a limit,
 * failed, ended. These frames change the run's state and the turn's terminal marker, not
 * its content.
 */

import { produce } from "solid-js/store";
import { CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL } from "~/lib/stream";
import type { RunEvent } from "~/lib/stream";
import { toast } from "~/ui";
import { toStats } from "../../data/summaries";
import { dropLiveFolds, FOLD_RUN_KIND } from "./compaction";
import type { FoldContext } from "./types";

/** Fold one lifecycle event; false when the event is not in this family. */
export function foldLifecycle(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId, announce } = c;
  const { patchById, setMessages } = deps;
  switch (ev.type) {
    case "run.error":
      if (announce) toast.error(ev.message || "The run failed.");
      patchById(assistantId, (m) => (m.streaming = false));
      deps.setErrored(true);
      deps.setRunQueue?.(null);
      return true;
    case "run.metrics":
      // The backend derives the window's fullness; the meter just renders it.
      // Authoritative either way: a null context (this turn ran on a windowless
      // model, or reported no usage) clears a stale reading rather than keeping it.
      // *Absent* is not null — a frame that carries no `context` field at all says
      // nothing about the window, and reading it as a clear would drop the meter on
      // a frame that merely was not about it.
      if (ev.context !== undefined) deps.setUsage(ev.context);
      deps.setStats(toStats(ev));
      return true;
    case "limit.notice":
      // A bound on the turn. "verify" is a transient "re-attempting…" progress note,
      // not a stop — leave it silent. "continue" says the turn went on past a bound by
      // itself, which the operator should know about but not be alarmed by. The rest
      // stopped the run, so surface why.
      if (announce && ev.limit === "continue") {
        toast.info(ev.message);
        return true;
      }
      //
      // "context" gets a sentence of our own on the end, and it is the only limit
      // that does: the backend's message says what the model refused and what the
      // chassis already tried, but the remedy the operator has *here* is a control on
      // the stopped turn, and only the frontend knows what that control is called.
      // Naming it in the toast is what connects the thing that just happened to the
      // button that answers it — the toast is transient, the marker is not.
      //
      // Only where the control is actually offered, though. A turn that already folded
      // and still overran carries the after-fold marker, `BlockedFooter` withholds the
      // button, and a toast naming it would contradict the sentence it is appended to.
      if (announce && ev.limit !== "verify")
        toast.error(
          ev.limit === "context" &&
            ev.detail !== CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL
            ? `${ev.message} Compact and retry on the stopped turn to fold this thread and carry on.`
            : ev.message,
        );
      return true;
    case "run.ended":
      // A blocked outcome is a real stopping point, not a normal finish —
      // leave a persistent marker on the turn (the limit.notice toast alone
      // vanishes, and a reload would otherwise show a turn that just stops).
      if (ev.outcome === "blocked")
        patchById(assistantId, (m) => {
          m.blocked = true;
          m.blockedDetail = ev.detail ?? undefined;
        });
      // A fold still in flight at the terminal never landed, so its turn goes: an
      // unsettled fold is not a fold, and the raw working it was showing is not
      // something to leave on screen as though it were the summary. Ordinarily there
      // is nothing to drop — a fold inside a chat turn settled long before the turn
      // ended — so this is the abandoned case, which is where the *reason* matters.
      setMessages(produce(dropLiveFolds));
      // The backend's own sentence, which is the only place the distinction lives:
      // "nothing new since the last fold", "the summarizer wrote nothing" and "the
      // conversation moved under it" are three different things to have happened and
      // lead to three different next moves.
      //
      // Guarded on the run's *kind*, not on whether a live turn was dropped. A fold
      // that declines before it announces itself has nothing to abandon — and that is
      // the single most likely way for this button to do nothing, so keying the report
      // on the abandoned turn would go quiet in exactly the case that needs a sentence.
      if (
        announce &&
        deps.state.runKind === FOLD_RUN_KIND &&
        ev.outcome === "blocked" &&
        ev.detail
      )
        toast.info(ev.detail);
      // Spent: the next run announces its own kind, and one attached past its
      // `run.started` must not inherit this one's.
      deps.state.runKind = null;
      deps.setRunClock?.(() => null);
      deps.setRunQueue?.(null);
      return true;
    case "run.queued":
      deps.setRunQueue?.({ endpoint: ev.endpoint, position: ev.position });
      return true;
    case "run.started":
      deps.state.runKind = ev.kind;
      deps.setRunQueue?.(null);
      deps.setRunClock?.(() => ({ startedAt: ev.ts, step: null }));
      return true;
    case "step.started":
      // Only onto a clock this run started: a reattach past `run.started` has no
      // start instant to pair the step with, and a step alone is not a clock.
      deps.setRunClock?.((prev) => prev && { ...prev, step: ev.index });
      return true;
    // step.completed: no store change
    default:
      return false;
  }
}
