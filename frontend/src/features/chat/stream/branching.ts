/**
 * What the operator can do to a turn that has already been recorded.
 *
 * Regenerate, edit, cycle versions, rewind, compact, delete, pin, bookmark a snapshot —
 * all of them act on the *conversation tree* rather than on the run in flight, which is
 * why they live apart from the controller that drives a run. Each is a thin relay to a
 * live endpoint, and each ends the same way: the backend returns the new active path and
 * the store is reseated from it, so what the transcript shows after the action is exactly
 * what a cold read would give.
 *
 * **Reseat, don't patch.** The two ops that stream (regenerate/edit) optimistically reset
 * the path *before* the run so the operator sees the turn clear immediately; the rest wait
 * for the backend's answer. Neither tries to compute the resulting tree locally — a second
 * implementation of the branch arithmetic is exactly the kind of thing that agrees with the
 * backend until the day it doesn't.
 *
 * **Never while a run is live.** A live fold keeps patching the message it was streaming
 * into, so an op that replaces the store while a run is open would leave the fold writing
 * to a message that no longer exists. Every one of these declines instead, the controls
 * are disabled with the same condition, and nothing here cancels a run on the operator's
 * behalf. A run this client is not driving reaches the backend's 409, which shows it.
 */

import { reconcile, type SetStoreFunction } from "solid-js/store";
import { type Setter } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { createInFlight } from "~/lib/inFlight";
import { effectiveSelection, type ModelSelection } from "~/lib/stores/models";
import { toast } from "~/ui";
import { toMessage } from "../data/messages";
import { toViewSnapshotRef } from "../data/viewSnapshots";
import type { ChatCreatedDTO, ConversationDetailDTO } from "../data/wire";
import { fetchRewindPreview, type RewindPreview } from "../data/rewind";
import type { ChatMessage, ContextUsage, ViewSnapshotRef } from "../model";
import {
  sendWithRestore,
  type RestoreChoice,
  type RestoreFiles,
} from "../rewindRestore";
import { nextId } from "./patch";

/** The store slots a conversation detail reseats — the active path, the window
 *  reading that moved with it, and the conversation-scoped snapshot history. */
export interface TranscriptStore {
  setMessages: SetStoreFunction<ChatMessage[]>;
  setUsage: Setter<ContextUsage | null>;
  setSnapshots: Setter<ViewSnapshotRef[]>;
}

/** Replace the transcript with the backend's active path, through the same mapper the
 *  cold read uses, so a reload, a version switch and a recovered stale decision all land
 *  on the identical shape. */
export function reseatFromDetail(
  store: TranscriptStore,
  detail: ConversationDetailDTO,
): void {
  store.setMessages(reconcile(detail.messages.map(toMessage)));
  // The active path moved (version switch / rewind / delete), so the window
  // state moves with it.
  store.setUsage(detail.context);
  // Re-seed the conversation-level snapshot history from the same detail.
  store.setSnapshots((detail.snapshots ?? []).map(toViewSnapshotRef));
}

/** Everything the branching ops need from the controller they belong to. Passed in
 *  rather than closed over, so the ops read as relays to the backend instead of as
 *  another part of the run-driving closure. */
export interface BranchingContext extends TranscriptStore {
  messages: ChatMessage[];
  /** The conversation the stream is bound to right now — a getter, because `send`
   *  adopts a freshly-created thread's id mid-flight. */
  conversationId: () => string | null;
  sending: () => boolean;
  setSending: (value: boolean) => void;
  patchById: (id: string, fn: (m: ChatMessage) => void) => void;
  /** The per-instance model override (the compare panes), or undefined when this
   *  stream follows the global picker. */
  overrideSelection: () => ModelSelection | null | undefined;
  driveRun: (runId: string, assistantId: string) => Promise<void>;
  reattachToLiveRun: (conversationId: string) => Promise<void>;
  /** Show the dry run of a file rewind and return the operator's choice. */
  askRestore: (
    preview: RewindPreview,
    changed: boolean,
  ) => Promise<RestoreChoice>;
  onTurnComplete?: () => void;
}

const NO_FILES: RewindPreview = {
  restore: [],
  delete: [],
  conflicts: [],
  unrestorable: [],
  previewToken: "",
};

function toastError(err: unknown, fallback: string): void {
  toast.error((err as { detail?: string })?.detail ?? fallback);
}

export function createBranchingOps(ctx: BranchingContext) {
  const reseat = (detail: ConversationDetailDTO) =>
    reseatFromDetail(ctx, detail);

  /**
   * Reseat, but only while the room is still on the thread the op was aimed at.
   *
   * A reseat is a whole-store replacement, so it has to be aimed. Every op here holds at
   * least one round trip, and the ones that stop a live run first hold a cancel as well
   * — long enough for the operator to pick another thread from the rail. Reseating then
   * writes the answering thread's transcript, its window reading and its snapshots over
   * whichever thread is now on screen, which reads as the room silently swapping its
   * contents. The backend still did the work; the thread that was left reads it back
   * when it is opened again.
   */
  const reseatIfCurrent = (
    conversationId: string,
    detail: ConversationDetailDTO,
  ): void => {
    if (ctx.conversationId() === conversationId) reseat(detail);
  };

  /** The threads a hand-started fold is running against.
   *
   *  Every other op here either streams (so the transcript animates) or returns in a
   *  round trip. A fold does neither: it holds a summarizer call inside it — seconds on
   *  a local model — and until it lands there is nothing anywhere in the room that has
   *  changed. Without this the operator picks "Compact now", the menu closes, and the
   *  product looks like it dropped the request.
   *
   *  Keyed by conversation, because this controller outlives every thread it binds to,
   *  so the fold belongs to a conversation rather than to the stream. That is the shape
   *  `createInFlight` exists for, and it also owns the claim-before-the-first-await
   *  ordering the fold depends on. */
  const compacting = createInFlight<string>();

  /** A 409 is a turn this client is not driving (one the inbox started, another tab):
   *  the controls only know this client's own runs. Say so, and show that turn rather
   *  than leaving the click unanswered. */
  function surfaceBusy(err: unknown): boolean {
    if (!isApiError(err) || err.status !== 409) return false;
    toast.warn("A turn is running in this thread. Stop it first.");
    ctx.setSending(false);
    const id = ctx.conversationId();
    if (id) void ctx.reattachToLiveRun(id);
    return true;
  }

  /** Post a regenerate or edit, offering to put the turn's file changes back first.
   *  Resolves null when the operator cancelled. A preview that cannot be read at all
   *  sends without one: the files are left as they are, which is what no token means. */
  async function postOffering(
    conversationId: string,
    messageId: string,
    post: (restore?: RestoreFiles) => Promise<ChatCreatedDTO>,
  ): Promise<ChatCreatedDTO | null> {
    let first = true;
    const outcome = await sendWithRestore({
      fetchPreview: async () => {
        try {
          return await fetchRewindPreview(conversationId, messageId);
        } catch (err) {
          if (!first) throw err;
          toast.warn(
            "File changes could not be checked. Continuing without restoring.",
          );
          return NO_FILES;
        } finally {
          first = false;
        }
      },
      ask: ctx.askRestore,
      send: post,
      isConflict: (err) => isApiError(err) && err.status === 409,
    });
    return outcome.sent ? outcome.value : null;
  }

  /** Re-answer an assistant turn from the preceding request, using the current
   *  model selection; the new answer becomes a sibling version. `messageId` is
   *  the assistant message's id. */
  async function regenerate(messageId: string): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    const i = ctx.messages.findIndex(
      (m) => m.id === messageId && m.role === "assistant",
    );
    if (i < 0) return;
    ctx.setSending(true);
    const override = ctx.overrideSelection();
    const sel = override ?? effectiveSelection();
    try {
      const created = await postOffering(conversationId, messageId, (restore) =>
        api.post<ChatCreatedDTO>("/chat/regenerate", {
          conversation_id: conversationId,
          message_id: messageId,
          endpoint_id: override?.endpointId,
          model: override?.model,
          restore_files: restore,
        }),
      );
      if (!created) {
        ctx.setSending(false);
        return;
      }
      const reset: ChatMessage = {
        id: messageId,
        role: "assistant",
        model: sel?.model,
        content: "",
        blocks: [],
        streaming: true,
        queued: true,
        createdAt: new Date().toISOString(),
      };
      ctx.setMessages(reconcile([...ctx.messages.slice(0, i), reset]));
      await ctx.driveRun(created.run_id, messageId);
    } catch (err) {
      if (surfaceBusy(err)) return;
      toastError(err, "Unable to regenerate the answer.");
      ctx.setSending(false);
    }
  }

  /** Re-ask an edited user turn as a new version; a fresh answer streams in.
   *  `messageId` is the user message's id. */
  async function edit(
    messageId: string,
    newText: string,
    selection?: ModelSelection | null,
    attachmentIds?: string[],
  ): Promise<void> {
    const conversationId = ctx.conversationId();
    const j = ctx.messages.findIndex(
      (m) => m.id === messageId && m.role === "user",
    );
    if (j < 0 || conversationId === null || ctx.sending()) return;
    // Reuse the turn's existing attachments unless the caller supplies a new set.
    const ids = attachmentIds ?? ctx.messages[j].attachmentIds ?? [];
    if (!newText.trim() && ids.length === 0) return;
    ctx.setSending(true);
    const override = selection ?? ctx.overrideSelection();
    const sel = override ?? effectiveSelection();
    const prompt = newText.trim();
    try {
      const created = await postOffering(conversationId, messageId, (restore) =>
        api.post<ChatCreatedDTO>("/chat/edit", {
          conversation_id: conversationId,
          message_id: messageId,
          prompt,
          endpoint_id: override?.endpointId,
          model: override?.model,
          attachment_ids: ids,
          restore_files: restore,
        }),
      );
      if (!created) {
        ctx.setSending(false);
        return;
      }
      const editedUser: ChatMessage = {
        ...ctx.messages[j],
        content: prompt,
        attachmentIds: ids.length ? ids : undefined,
      };
      const assistantId = nextId("a");
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        model: sel?.model,
        content: "",
        blocks: [],
        streaming: true,
        queued: true,
        createdAt: new Date().toISOString(),
      };
      ctx.setMessages(
        reconcile([...ctx.messages.slice(0, j), editedUser, assistantMsg]),
      );
      await ctx.driveRun(created.run_id, assistantId);
    } catch (err) {
      if (surfaceBusy(err)) return;
      toastError(err, "Unable to submit the edit.");
      ctx.setSending(false);
    }
  }

  /** Switch a turn to a sibling version; reseat the store from the returned
   *  active path and refresh the sidebar. */
  async function switchVersion(
    messageId: string,
    index: number,
  ): Promise<void> {
    const conversationId = ctx.conversationId();
    // The reseat below replaces the store, and a still-streaming foldEvent would keep
    // patching a message it removed; the controls are disabled while a run is live.
    if (conversationId === null || ctx.sending()) return;
    try {
      const detail = await api.post<ConversationDetailDTO>(
        `/conversations/${conversationId}/messages/${messageId}/version`,
        { index },
      );
      // `onTurnComplete` stays unconditional: it refreshes the rail, which is about
      // the thread that answered rather than the one on screen.
      reseatIfCurrent(conversationId, detail);
      ctx.onTurnComplete?.();
    } catch (err) {
      toastError(err, "Unable to switch versions.");
    }
  }

  /** Rewind the thread to end at a turn; the operator's next send branches. */
  async function rewind(messageId: string): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    try {
      const detail = await api.post<ConversationDetailDTO>(
        `/conversations/${conversationId}/messages/${messageId}/rewind`,
        {},
      );
      reseatIfCurrent(conversationId, detail);
      ctx.onTurnComplete?.();
      // The toast is unconditional for the same reason the refresh is: the rewind
      // happened, and the operator asked for it, whichever thread they are looking at
      // by the time it lands.
      toast.success("Rewound — your next message starts a new branch");
    } catch (err) {
      toastError(err, "Unable to rewind the conversation.");
    }
  }

  /** Fold this thread's older turns into a summary now, rather than waiting for it to
   *  reach the automatic threshold — for a thread the operator knows is about to need the
   *  room.
   *
   *  It drives a run, like a turn does, so the frames an automatic fold produces stream
   *  here too and `fold.ts` renders them without knowing which trigger fired.
   *
   *  Nothing is reseated afterwards. `conversation.compacted` carries the checkpoint and
   *  where it goes, so the transcript is already right; refetching it would replace a
   *  store the operator just watched fill with an identical one. */
  async function compactNow(): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    // The claim is taken before the first await: a guard read after an await is not a
    // guard. The failure is reported inside the task so `run` resolves rather than
    // rejecting — every caller of a fold treats it as fire-and-forget.
    await compacting.run(conversationId, async () => {
      try {
        // Guarded like any other run the room drives, so a send during the fold
        // queues instead of starting a turn.
        ctx.setSending(true);
        const started = await api.post<ChatCreatedDTO>(
          `/conversations/${conversationId}/compact`,
          {},
        );
        // No anchor message: a fold emits no answer and no thinking, so nothing it
        // sends is addressed to a turn. The compaction frames seat their own turn in
        // the transcript, which is exactly what lets this path have no assistant
        // bubble to hang off.
        await ctx.driveRun(started.run_id, "");
      } catch (err) {
        ctx.setSending(false);
        toastError(err, "Unable to compact this conversation.");
      }
    });
  }

  /** Delete a turn and everything after it; reseat from the returned active path
   *  (the DELETE returns the post-delete detail, like version-switch/rewind). */
  async function removeMessage(messageId: string): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    try {
      const detail = await api.del<ConversationDetailDTO>(
        `/conversations/${conversationId}/messages/${messageId}`,
      );
      reseatIfCurrent(conversationId, detail);
      ctx.onTurnComplete?.();
    } catch (err) {
      toastError(err, "Unable to delete the message.");
    }
  }

  /** Pin/unpin a turn. The backend owns the flag; this optimistically echoes the
   *  toggle and reverts if the POST fails. */
  async function toggleMessagePin(messageId: string): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null) return;
    const msg = ctx.messages.find((m) => m.id === messageId);
    if (!msg) return;
    const next = !msg.pinned;
    ctx.patchById(messageId, (m) => {
      m.pinned = next;
    });
    try {
      await api.post(
        `/conversations/${conversationId}/messages/${messageId}/pin`,
        { pinned: next },
      );
    } catch (err) {
      ctx.patchById(messageId, (m) => {
        m.pinned = !next;
      });
      toastError(err, "Unable to update the pin.");
    }
  }

  /** Flip a snapshot's keeper bookmark: optimistically replace the array element
   *  (a spread copy — never mutate the stored ref in place), then confirm against
   *  the backend, reverting the same way on failure. */
  async function toggleSnapshotKeeper(
    snapshotId: string,
    keeper: boolean,
  ): Promise<void> {
    ctx.setSnapshots((prev) =>
      prev.map((s) => (s.snapshotId === snapshotId ? { ...s, keeper } : s)),
    );
    try {
      await api.post(`/views/snapshots/${snapshotId}/keeper`, { keeper });
    } catch (err) {
      ctx.setSnapshots((prev) =>
        prev.map((s) =>
          s.snapshotId === snapshotId ? { ...s, keeper: !keeper } : s,
        ),
      );
      toastError(err, "Unable to update the keeper flag.");
    }
  }

  return {
    regenerate,
    edit,
    switchVersion,
    rewind,
    compactNow,
    /** True while a hand-started fold is running against that conversation. The stream
     *  turns it into the room's own boolean by pairing it with the reactive bound id. */
    isCompacting: compacting.has,
    removeMessage,
    toggleMessagePin,
    toggleSnapshotKeeper,
  };
}
