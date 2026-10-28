/**
 * Reconciling the optimistic transcript with what the backend actually persisted.
 *
 * Five moments need it and they are all the same move: read the conversation's detail,
 * and act on what came back. What differs is only *what* they do with it — reseat the
 * transcript from it after a run ends, re-attach to a run that turned out to be live,
 * reseat wholesale after a decision was made somewhere else, or find the row a run is
 * answering.
 *
 * **The backend's rows are the transcript.** A turn's prompt is persisted when the turn
 * begins, and everything else when it finishes, so at the end of a run the detail holds
 * the whole turn under ids the backend minted. This used to adopt those ids onto the
 * optimistic messages by *position*, which only worked while the two lists happened to
 * line up — and quietly addressed an id the backend did not know when they did not. Now
 * the transcript is reseated from the detail, and the optimistic echo (the operator's
 * own words, shown before the backend had answered) is simply replaced by the row the
 * backend confirmed.
 *
 * **The guard is why they live together.** Every one of these has an `await` in the
 * middle of it, and the store it writes into belongs to whatever thread is open when the
 * answer arrives — not to the one that was open when the request went out. Reseating one
 * thread's history over another's is not a cosmetic slip: it replaces the transcript of
 * a turn that is still streaming and freezes it. Written per caller, that guard came out
 * three slightly different ways and was missing from the fourth. Written once, it is the
 * only way to get at a detail at all.
 */

import { toActiveRun } from "../data/summaries";
import type { ConversationDetailDTO } from "../data/wire";
import type { ActiveRun, ChatMessage } from "../model";
import { attachOptions, type ReattachOptions } from "./drive";

export interface ResumeDeps {
  /** The thread the stream is bound to *right now* — read again after every await. */
  conversationId: () => string | null;
  /** How a conversation's persisted detail is read. Handed in rather than reached
   *  for, so the guard above is exercisable against a read that resolves whenever
   *  the test says it does — which is the only way to write down what happens when
   *  the operator switches threads mid-flight. */
  fetchDetail: (id: string) => Promise<ConversationDetailDTO>;
  messages: ChatMessage[];
  /** Replace the transcript wholesale from a persisted detail. */
  reseat: (detail: ConversationDetailDTO) => void;
  /** Replace only the messages from a persisted detail — what a finished run does, since
   *  the window reading and the snapshots were kept current by its own events. */
  reseatMessages: (detail: ConversationDetailDTO) => void;
  reattachRun: (runId: string, opts: ReattachOptions) => Promise<void>;
}

export interface ResumeOps {
  reseatAfterRun: () => Promise<void>;
  reattachToLiveRun: (conversationId: string) => Promise<void>;
  reconcileStaleDecision: (
    opts?: Pick<ReattachOptions, "seat">,
  ) => Promise<void>;
  recoverLostRun: () => Promise<void>;
  /** Re-read the thread and return the run (if it is still live) that is answering. */
  refetchRun: (runId: string) => Promise<ActiveRun | null>;
}

export function createResumeOps(deps: ResumeDeps): ResumeOps {
  /** Every read below goes through here. Best-effort throughout: a failed read leaves
   *  the optimistic store in place and the next navigation or turn reconciles it. */
  const withFreshDetail = async (
    expected: string | null,
    fn: (detail: ConversationDetailDTO) => void | Promise<void>,
  ): Promise<void> => {
    if (expected === null) return;
    let detail: ConversationDetailDTO;
    try {
      detail = await deps.fetchDetail(expected);
    } catch {
      return;
    }
    // The operator switched threads while the read was in flight. Whatever this
    // answer says is about a conversation that is no longer on screen.
    if (deps.conversationId() !== expected) return;
    await fn(detail);
  };

  /** A run has ended: take the backend's account of the turn. Every id, version count,
   *  pin and stop marker comes from the rows it returns, so a later edit, pin or
   *  regenerate addresses an id the backend knows — and a turn that stopped with only
   *  its prompt on record (a cancel before the first token) shows exactly that, rather
   *  than an answer that never existed. */
  async function reseatAfterRun(): Promise<void> {
    const lenAtStart = deps.messages.length;
    await withFreshDetail(deps.conversationId(), (detail) => {
      // The store moved under us: the operator started another turn (the composer
      // re-enabled the instant streaming stopped). Reseating now would write over the
      // new turn's optimistic messages and freeze its stream; that turn reseats itself
      // when it completes.
      if (deps.messages.length !== lenAtStart) return;
      deps.reseatMessages(detail);
    });
  }

  /** When the backend has a run active on this conversation that this client is not
   *  driving (a second tab/device, a stale UI) look up the conversation's current
   *  active run and reattach to it, so the turn that's actually in flight becomes
   *  visible instead of silently going nowhere. */
  async function reattachToLiveRun(conversationId: string): Promise<void> {
    await withFreshDetail(conversationId, async (detail) => {
      const ar = toActiveRun(detail.active_run);
      if (ar) await deps.reattachRun(ar.id, attachOptions(ar));
    });
  }

  /** After a submitted approval/host-command decision 409s (the run had already
   *  resumed elsewhere — a second tab, a retried request — by the time this one
   *  landed), the pending card's decision is moot. Refetch so the transcript
   *  reconciles with whatever the winning decision actually did, re-attaching to the
   *  run if it's still in flight. Unlike `reattachToLiveRun`, this always reseats —
   *  the winning decision may have already finished the run entirely, not just still
   *  be running. */
  async function reconcileStaleDecision(
    opts: Pick<ReattachOptions, "seat"> = {},
  ): Promise<void> {
    await withFreshDetail(deps.conversationId(), async (detail) => {
      deps.reseat(detail);
      const ar = toActiveRun(detail.active_run);
      if (ar) await deps.reattachRun(ar.id, { ...attachOptions(ar), ...opts });
    });
  }

  /** A reattach that seeded a turn and then folded nothing means the run was gone
   *  (evicted, or lost to a server restart): render what the backend has, which is the
   *  prompt and whatever of the turn was recorded — the thread is never reset to empty
   *  on the strength of a run that cannot be replayed. */
  async function recoverLostRun(): Promise<void> {
    await withFreshDetail(deps.conversationId(), deps.reseat);
  }

  /** A reattach found the run's prompt missing from the transcript it holds — the run
   *  began after this thread was loaded, or the prompt was still being recorded when
   *  the detail was read. Re-read once and reseat, so the row exists to seat the
   *  streaming answer after, and hand back the run as the backend now describes it. */
  async function refetchRun(runId: string): Promise<ActiveRun | null> {
    let found: ActiveRun | null = null;
    await withFreshDetail(deps.conversationId(), (detail) => {
      deps.reseat(detail);
      const ar = toActiveRun(detail.active_run);
      if (ar?.id === runId) found = ar;
    });
    return found;
  }

  return {
    reseatAfterRun,
    reattachToLiveRun,
    reconcileStaleDecision,
    recoverLostRun,
    refetchRun,
  };
}
