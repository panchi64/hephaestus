/**
 * Reconciling the optimistic transcript with what the backend actually persisted.
 *
 * Four moments need it and they are all the same move: read the conversation's detail,
 * and act on what came back. What differs is only *what* they do with it — adopt the
 * server's ids onto a turn that just finished, re-attach to a run that turned out to be
 * live, or reseat wholesale after a decision was made somewhere else.
 *
 * **The guard is why they live together.** Every one of these has an `await` in the
 * middle of it, and the store it writes into belongs to whatever thread is open when the
 * answer arrives — not to the one that was open when the request went out. Reseating one
 * thread's history over another's is not a cosmetic slip: it replaces the transcript of
 * a turn that is still streaming and freezes it. Written per caller, that guard came out
 * three slightly different ways and was missing from the fourth. Written once, it is the
 * only way to get at a detail at all.
 */

import { produce, type SetStoreFunction } from "solid-js/store";
import { toActiveRun } from "../data/summaries";
import type { ConversationDetailDTO } from "../data/wire";
import type { ChatMessage } from "../model";
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
  setMessages: SetStoreFunction<ChatMessage[]>;
  /** Replace the transcript wholesale from a persisted detail. */
  reseat: (detail: ConversationDetailDTO) => void;
  reattachRun: (runId: string, opts: ReattachOptions) => Promise<void>;
  /** Whether the operator cancelled the turn that just ended. */
  wasCancelled: () => boolean;
}

export interface ResumeOps {
  adoptServerMeta: () => Promise<void>;
  reattachToLiveRun: (conversationId: string) => Promise<void>;
  reconcileStaleDecision: () => Promise<void>;
  recoverLostRun: () => Promise<void>;
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

  /** Reconcile the live store with the backend's projected active path after a turn:
   *  adopt each turn's real node id + version index/count + pin by position (the store
   *  mirrors the same active path), leaving live-only fields the cold projection
   *  doesn't carry — `preview`, `runId` — untouched. A length mismatch (e.g. a turn
   *  that produced no persisted answer) falls back to a full reseat. */
  async function adoptServerMeta(): Promise<void> {
    const lenAtStart = deps.messages.length;
    await withFreshDetail(deps.conversationId(), (detail) => {
      // The store moved under us: the operator started another turn (the composer
      // re-enabled the instant streaming stopped). Reconciling now would reseat over
      // the new turn's optimistic messages and freeze its stream; that turn
      // reconciles itself when it completes.
      if (deps.messages.length !== lenAtStart) return;
      const server = detail.messages;
      if (server.length !== deps.messages.length) {
        // A shorter backend history normally means a turn produced no persisted
        // answer, so reseat to drop the optimistic turn. But a *cancelled* turn also
        // persists nothing — there reseating would discard the in-flight turn the
        // operator chose to keep (and blank a brand-new chat), so leave the store as
        // is; the next completed turn reconciles it.
        if (!deps.wasCancelled()) deps.reseat(detail);
        return;
      }
      deps.setMessages(
        produce((list) => {
          for (let i = 0; i < list.length; i++) {
            list[i].id = server[i].id;
            list[i].versionIndex = server[i].version_index;
            list[i].versionCount = server[i].version_count;
            list[i].pinned = server[i].pinned;
            // The store owns the stop marker (a continue retires it), so take its
            // word here too — otherwise a turn cleared server-side stays warned
            // locally until a full reseat.
            list[i].blocked = server[i].blocked_reason != null;
            list[i].blockedDetail = server[i].blocked_reason ?? undefined;
          }
        }),
      );
    });
  }

  /** After a 409 (the backend already has a run active on this conversation — a
   *  parallel submit, a stale UI, or a second tab/device) look up the conversation's
   *  current active run and reattach to it, so the turn that's actually in flight
   *  becomes visible instead of silently going nowhere. */
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
  async function reconcileStaleDecision(): Promise<void> {
    await withFreshDetail(deps.conversationId(), async (detail) => {
      deps.reseat(detail);
      const ar = toActiveRun(detail.active_run);
      if (ar) await deps.reattachRun(ar.id, attachOptions(ar));
    });
  }

  /** A reattach that seeded a turn and then folded nothing means the run was gone
   *  (evicted, or lost to a server restart): fall back to the persisted thread so a
   *  finished answer still shows rather than a blank assistant turn. */
  async function recoverLostRun(): Promise<void> {
    await withFreshDetail(deps.conversationId(), deps.reseat);
  }

  return {
    adoptServerMeta,
    reattachToLiveRun,
    reconcileStaleDecision,
    recoverLostRun,
  };
}
