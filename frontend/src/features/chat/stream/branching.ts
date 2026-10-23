/**
 * Operations on already-recorded turns (regenerate, edit, versions, rewind, compact,
 * delete, pin, snapshot keeper). Each is a thin relay to an endpoint that returns the new
 * active path, and the store is reseated from it.
 *
 * Reseat, don't patch: regenerate/edit optimistically reset the path before streaming,
 * the rest wait for the backend. None compute the resulting tree locally.
 *
 * Every op declines while a run is live, since a live fold would keep writing to a
 * replaced message. The controls are disabled on the same condition and nothing here
 * cancels a run; a run this client is not driving reaches the backend's 409.
 */

import { reconcile, type SetStoreFunction } from "solid-js/store";
import { type Setter } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { createInFlight } from "~/lib/inFlight";
import { effectiveSelection, type ModelSelection } from "~/lib/stores/models";
import { confirm, toast } from "~/ui";
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

/** Replace the transcript with the backend's active path, via the cold read's mapper. */
export function reseatFromDetail(
  store: TranscriptStore,
  detail: ConversationDetailDTO,
): void {
  store.setMessages(reconcile(detail.messages.map(toMessage)));
  store.setUsage(detail.context);
  store.setSnapshots((detail.snapshots ?? []).map(toViewSnapshotRef));
}

/** What the branching ops need from their controller. */
export interface BranchingContext extends TranscriptStore {
  messages: ChatMessage[];
  /** A getter, because `send` adopts a new thread's id mid-flight. */
  conversationId: () => string | null;
  sending: () => boolean;
  setSending: (value: boolean) => void;
  patchById: (id: string, fn: (m: ChatMessage) => void) => void;
  /** Per-instance model override (compare panes), or undefined to follow the picker. */
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

function toastError(err: unknown, fallback: string): void {
  toast.error((err as { detail?: string })?.detail ?? fallback);
}

export function createBranchingOps(ctx: BranchingContext) {
  const reseat = (detail: ConversationDetailDTO) =>
    reseatFromDetail(ctx, detail);

  /** Reseat only while the room is still on the thread the op targeted; the round trip
   *  is long enough for the operator to switch threads, and a late reseat would
   *  overwrite whichever thread is now on screen. */
  const reseatIfCurrent = (
    conversationId: string,
    detail: ConversationDetailDTO,
  ): void => {
    if (ctx.conversationId() === conversationId) reseat(detail);
  };

  /** Threads with a hand-started fold running. A fold holds a summarizer call (seconds
   *  on a local model) with nothing else visibly changing, so the room needs this to
   *  show it is working. Keyed by conversation because the controller outlives threads;
   *  `createInFlight` also owns the claim-before-first-await ordering. */
  const compacting = createInFlight<string>();

  /** A 409 means a turn this client is not driving (inbox-started, another tab); warn
   *  and attach to it. */
  function surfaceBusy(err: unknown): boolean {
    if (!isApiError(err) || err.status !== 409) return false;
    toast.warn("A turn is running in this thread. Stop it first.");
    ctx.setSending(false);
    const id = ctx.conversationId();
    if (id) void ctx.reattachToLiveRun(id);
    return true;
  }

  /** Set while a regenerate or edit is choosing about files, before `sending` is. */
  let offering = false;

  /** Post a regenerate or edit after offering to restore the turn's file changes;
   *  null when cancelled. `sending` is set only once the post starts, so a dialog does
   *  not lock the room. */
  async function postOffering(
    conversationId: string,
    messageId: string,
    post: (restore?: RestoreFiles) => Promise<ChatCreatedDTO>,
  ): Promise<ChatCreatedDTO | null> {
    offering = true;
    try {
      const outcome = await sendWithRestore({
        fetchPreview: () => fetchRewindPreview(conversationId, messageId),
        ask: ctx.askRestore,
        send: (restore) => {
          ctx.setSending(true);
          return post(restore);
        },
        isMissing: (err) => isApiError(err) && err.status === 404,
        confirmUnchecked: () =>
          confirm({
            title: "Couldn't check file changes",
            detail: "Files this turn changed may not be put back if you go on.",
            confirmLabel: "Continue without restoring",
          }),
      });
      return outcome.sent ? outcome.value : null;
    } finally {
      offering = false;
    }
  }

  /** Re-answer an assistant turn (`messageId`) as a sibling version, using the current
   *  model selection. */
  async function regenerate(messageId: string): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending() || offering) return;
    const i = ctx.messages.findIndex(
      (m) => m.id === messageId && m.role === "assistant",
    );
    if (i < 0) return;
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
      if (!created) return;
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

  /** Re-ask an edited user turn (`messageId`) as a new version. False when not submitted
   *  (cancelled, refused or failed) so the caller keeps the draft. */
  async function edit(
    messageId: string,
    newText: string,
    selection?: ModelSelection | null,
    attachmentIds?: string[],
  ): Promise<boolean> {
    const conversationId = ctx.conversationId();
    const j = ctx.messages.findIndex(
      (m) => m.id === messageId && m.role === "user",
    );
    if (j < 0 || conversationId === null || ctx.sending() || offering)
      return false;
    const ids = attachmentIds ?? ctx.messages[j].attachmentIds ?? [];
    if (!newText.trim() && ids.length === 0) return false;
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
      if (!created) return false;
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
      return true;
    } catch (err) {
      if (surfaceBusy(err)) return false;
      toastError(err, "Unable to submit the edit.");
      ctx.setSending(false);
      return false;
    }
  }

  /** Switch a turn to a sibling version and refresh the sidebar. */
  async function switchVersion(
    messageId: string,
    index: number,
  ): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    try {
      const detail = await api.post<ConversationDetailDTO>(
        `/conversations/${conversationId}/messages/${messageId}/version`,
        { index },
      );
      // Unconditional: the rail refresh concerns the thread that answered.
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
      // Unconditional, like the refresh: the rewind happened whichever thread is on screen.
      toast.success("Rewound — your next message starts a new branch");
    } catch (err) {
      toastError(err, "Unable to rewind the conversation.");
    }
  }

  /** Fold older turns into a summary now instead of at the automatic threshold.
   *  It drives a run, so `fold.ts` renders its frames whichever trigger fired. Nothing is
   *  reseated afterwards: `conversation.compacted` already carries the checkpoint. */
  async function compactNow(): Promise<void> {
    const conversationId = ctx.conversationId();
    if (conversationId === null || ctx.sending()) return;
    // The claim is taken before the first await. Failure is reported inside the task so
    // `run` resolves; callers treat a fold as fire-and-forget.
    await compacting.run(conversationId, async () => {
      try {
        // A send during the fold queues instead of starting a turn.
        ctx.setSending(true);
        const started = await api.post<ChatCreatedDTO>(
          `/conversations/${conversationId}/compact`,
          {},
        );
        // No anchor message: the compaction frames seat their own turn.
        await ctx.driveRun(started.run_id, "");
      } catch (err) {
        ctx.setSending(false);
        toastError(err, "Unable to compact this conversation.");
      }
    });
  }

  /** Delete a turn and everything after it. */
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

  /** Pin/unpin a turn, optimistically; reverts if the POST fails. */
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

  /** Flip a snapshot's keeper bookmark optimistically (replace the element with a
   *  copy, never mutate in place); reverts on failure. */
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
    /** True while a hand-started fold is running against that conversation. */
    isCompacting: compacting.has,
    removeMessage,
    toggleMessagePin,
    toggleSnapshotKeeper,
  };
}
