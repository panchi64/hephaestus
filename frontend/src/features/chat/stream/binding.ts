/**
 * Pointing the stream at a different thread.
 *
 * One effect, and everything it has to get right is about a *transition* rather than about
 * any thread in particular: what the previous thread leaves behind, what the new one is
 * seeded from, and which of the two a late answer belongs to. That is a different question
 * from driving a run or sending a turn, and it went wrong in three separate ways while it
 * was a paragraph inside the controller.
 *
 * **The live store is authoritative for the thread already open.** A turn's messages are
 * persisted only when it finishes, so a refetch mid-stream reads an *empty* conversation.
 * Re-seeding from that would blank a streaming thread, so a source change for the thread
 * we are already on is ignored outright — which also spares the freshly-created thread an
 * empty flash while its history loads, and keeps the live-only fields (preview, artifacts,
 * runId) the cold projection does not carry.
 *
 * **A null key is not "the same thread with no id".** Solid retains a resource's last
 * value once its source goes null, so the `source` arriving alongside a null key is the
 * history of the thread just *left* — seeding from it puts a deleted or abandoned
 * conversation back on screen. A null key always starts empty.
 *
 * **The backfill races the stream and must lose.** Opening a thread mid-turn fires a plan
 * read against state the run is still mutating; the fetch answers with what was true
 * before. So it re-checks that the operator is still on the thread it asked about *and*
 * that the stream has not already said something newer — otherwise the slower answer wins
 * and the panel is stale until a mutation that may never come.
 */

import { createEffect } from "solid-js";
import { reconcile, type SetStoreFunction } from "solid-js/store";
import type { TaskItem } from "~/lib/stream";
import { fetchPlan, fetchTasks } from "../data/conversations";
import type {
  ChatMessage,
  ContextUsage,
  ConversationStats,
  PlanDocument,
  ViewSnapshotRef,
} from "../model";
import type { FoldState } from "./fold";

/** Everything a thread switch resets or re-seeds. */
export interface BindingDeps {
  /** The thread to bind to — null for a new, unsaved conversation. */
  key: () => string | null;
  /** Its persisted history, or undefined while the read is in flight. */
  initial: () => ChatMessage[] | undefined;
  messages: ChatMessage[];
  /** The thread the stream is bound to *right now* — which lags `key` while a
   *  freshly-created conversation is adopting its backend id. */
  boundTo: () => string | null;
  /** Bind: the controller records the new thread and forgets which run it has
   *  already reattached to, so returning to a still-live thread can reattach again. */
  onBind: (conversationId: string | null) => void;
  /** Take the transport away from the drive in flight — the thread it was folding
   *  into is no longer on screen. */
  supersede: () => void;
  setSending: (value: boolean) => void;
  /** The run-scoped fold bookkeeping, reset here so the previous run's seqs can't
   *  suppress the next one's events. */
  state: FoldState;
  setMessages: SetStoreFunction<ChatMessage[]>;
  setUsage: (usage: ContextUsage | null) => void;
  setStats: (stats: ConversationStats | null) => void;
  setSnapshots: (snapshots: ViewSnapshotRef[]) => void;
  setTasks: (items: TaskItem[]) => void;
  setPlan: (plan: PlanDocument | null) => void;
  /** The loaded thread's reconstructed window/readout/snapshot state, if the caller
   *  has any — a new conversation has none. */
  initialContext?: () => ContextUsage | null | undefined;
  initialStats?: () => ConversationStats | null | undefined;
  initialSnapshots?: () => ViewSnapshotRef[] | undefined;
}

const INIT = Symbol("init");

export function createThreadBinding(deps: BindingDeps): void {
  let lastKey: string | null | typeof INIT = INIT;
  let lastSource: ChatMessage[] | undefined | typeof INIT = INIT;

  createEffect(() => {
    const k = deps.key();
    const source = deps.initial();
    if (k === lastKey && source === lastSource) return;
    // Record the transition before any early return below, or the bookkeeping
    // goes stale: skipping these on the authoritative-store guard left `lastKey`
    // pinned at its pre-adoption value, so a later transition back to that value
    // (e.g. compare's teardown reverting the key to null) read as "no change"
    // and the transcript never cleared.
    lastKey = k;
    lastSource = source;
    if (k === deps.boundTo() && deps.messages.length > 0) return;
    deps.supersede();
    deps.setSending(false);
    // A new thread starts a fresh event sequence; drop the prior run's fold/resume
    // bookkeeping so its seqs don't suppress the next run's events.
    deps.state.maxFoldedSeq = 0;
    deps.state.replayThrough = 0;
    deps.state.activeRunId = null;
    deps.state.runKind = null;
    deps.state.foldTarget = null;
    deps.onBind(k);
    seed(k, source);
  });

  /** Everything the newly-bound thread starts with, and the two reads that fill in
   *  what the stream alone cannot know. */
  function seed(k: string | null, source: ChatMessage[] | undefined): void {
    deps.setMessages(reconcile(k === null ? [] : source ? source.slice() : []));
    // Seed the meter from the loaded thread's reconstructed state (null for a new
    // conversation, or one whose usage/window couldn't be determined).
    deps.setUsage(k === null ? null : (deps.initialContext?.() ?? null));
    // Seeded from the loaded thread, not cleared: the backend rebuilds the readout
    // from the stored messages, so an existing conversation reports what it has spent
    // before its next turn runs rather than starting the line blank.
    deps.setStats(k === null ? null : (deps.initialStats?.() ?? null));
    // Seed the git-style snapshot history from the loaded thread (empty for a new
    // conversation); the live `view.snapshot` event appends to it from here.
    deps.setSnapshots(k === null ? [] : (deps.initialSnapshots?.() ?? []));
    // Both are owned by the backend and survive reloads, so a thread switch clears the
    // old thread's and refetches rather than carrying either over.
    deps.setTasks([]);
    deps.setPlan(null);
    if (k === null) return;
    backfill(deps, k, "tasksRevision", fetchTasks, deps.setTasks);
    backfill(deps, k, "planRevision", fetchPlan, deps.setPlan);
  }
}

/** Re-seed one backend-owned surface from REST, unless the stream got there first.
 *
 * The counter snapshot is the whole of it. Opening a thread whose run is mid-turn races
 * the backfill against the live event, and the fetch answers with pre-mutation state —
 * without this the slower answer wins and the panel is stale until a mutation that may
 * never come. Each surface counts its own updates (`FoldState`), so a plan arriving does
 * not make the task-list backfill think it had been overtaken.
 *
 * Generic because there are two of these and they differ only in which counter, which
 * fetch and which setter — and a second hand-written copy of a race guard is a race guard
 * that will be fixed in one place and not the other.
 */
function backfill<T>(
  deps: BindingDeps,
  conversationId: string,
  counter: "tasksRevision" | "planRevision",
  fetch: (id: string) => Promise<T>,
  apply: (value: T) => void,
): void {
  const seenAtRequest = deps.state[counter];
  void fetch(conversationId)
    .then((value) => {
      // Drop it if the operator has since left the thread, or the stream already said
      // something newer.
      if (
        deps.key() === conversationId &&
        deps.state[counter] === seenAtRequest
      )
        apply(value);
    })
    .catch(() => {
      // The panel is an aid, not the transcript — a failed backfill leaves it empty and
      // the next event fills it in.
    });
}
