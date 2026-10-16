/**
 * A message sent into a run that is already going.
 *
 * The ordinary send starts a turn. These four do not: they queue text onto a run in
 * flight, rewrite it while it is still queued, withdraw it before the model sees it, or —
 * when the run ended without ever reaching it — hand it back to the composer. One
 * concern, and it is not the drive's: the run is unaffected either way, so nothing here
 * has any business inside the code that opens and tears down a reader.
 *
 * **The operator's words are never dropped.** That is the whole of the invariant these
 * share, and the reason the undelivered draft lives here rather than in the controller.
 * Every path that can end with typed text and no turn to carry it — a POST refused, a run
 * that went terminal first, a queued bubble the run never consumed, a cancel — routes
 * through `stash`, and the composer prefills from it.
 *
 * **No optimistic edit on a message the model may already have read.** A withdraw or a
 * rewrite races the run's next boundary, and the backend answers 404 when it lost. The
 * bubble therefore changes only *after* the backend accepts: what the model actually saw
 * is what the transcript has to keep showing.
 */

import { createSignal, type Accessor } from "solid-js";
import { produce, reconcile, type SetStoreFunction } from "solid-js/store";
import { api, isApiError } from "~/lib/api";
import type { ModelSelection } from "~/lib/stores/models";
import { toast } from "~/ui";
import type { ChatCreatedDTO } from "../data/wire";
import type { ChatMessage } from "../model";
import { forgetQueuedDraft, queuedDraft } from "../queuedDraft";
import { nextId, type PatchById } from "./patch";

/** What the steering ops need from the controller they belong to. Handed in rather
 *  than closed over, so each one reads as a relay to the backend plus the bubble it
 *  owns — and so the queue races below can be exercised without a live run. */
export interface SteeringDeps {
  messages: ChatMessage[];
  setMessages: SetStoreFunction<ChatMessage[]>;
  patchById: PatchById;
  /** The thread the stream is bound to right now — null while it is still unsaved,
   *  which is the one case a queued message has nothing to queue against. */
  conversationId: () => string | null;
  /** Adopt the id the backend minted when a queue attempt turned into a fresh turn. */
  adoptConversationId: (id: string) => void;
  /** The run a queued message rides, or null. */
  activeRunId: () => string | null;
  setSending: (value: boolean) => void;
  /** The model a promoted bubble names. Display only — the backend resolves the
   *  binding it actually runs on — and already resolved by the controller, which
   *  settles the same override-or-picker question for an ordinary send. */
  selection: () => ModelSelection | null | undefined;
  driveRun: (runId: string, assistantId: string) => Promise<void>;
}

export interface SteeringOps {
  /** Text of queued messages that were never delivered — the composer prefills
   *  from it, then clears it. */
  undeliveredDraft: Accessor<string | null>;
  clearUndeliveredDraft: () => void;
  /** Hand text back to the composer rather than dropping it. */
  stash: (text: string) => void;
  /** Drop any still-pending steering bubbles and stash their text. */
  restoreUndelivered: () => void;
  /** Resolves false when the backend did not take the message, so the composer can put
   *  it back — the same rule as a fresh turn's send. */
  sendWhileStreaming: (text: string) => Promise<boolean>;
  withdrawQueued: (queuedMessageId: string) => Promise<void>;
  editQueued: (queuedMessageId: string, text: string) => Promise<void>;
  /** Ask the run to hold a queued message back, or to stop holding it. Taken while the
   *  operator has it open in an editor, released when they save or give up. */
  holdQueued: (queuedMessageId: string, held: boolean) => Promise<void>;
}

export function createSteeringOps(deps: SteeringDeps): SteeringOps {
  // Text of steering messages that were still queued when their run reached
  // terminal (cancel/error/timeout) — never delivered to the model. The screen
  // hands it back to the composer as a prefill so the operator's words aren't
  // lost; cleared once consumed.
  const [undeliveredDraft, setUndeliveredDraft] = createSignal<string | null>(
    null,
  );

  const stash = (text: string): void => {
    setUndeliveredDraft((prev) => (prev ? `${prev}\n${text}` : text));
  };

  /** Drop any still-pending steering bubbles and hand their text back to the
   *  composer (`undeliveredDraft`). Idempotent — a no-op when nothing is
   *  pending — so the drive teardown and `cancel` can both call it safely.
   *
   *  Only the operator's own. A sub-agent's report queues on the same road and can be
   *  pending at the same moment, and it is neither theirs to have back nor lost: the
   *  backend re-delivers a report the run dropped as a turn of its own. Putting it in
   *  their input box would hand them words they never wrote to send back to the agent
   *  that had already been told them. */
  function restoreUndelivered(): void {
    const mine = (m: ChatMessage): boolean =>
      m.queuedPending === true && m.role === "user";
    const leftovers = deps.messages.filter(mine);
    if (leftovers.length === 0) return;
    deps.setMessages(reconcile(deps.messages.filter((m) => !mine(m))));
    // What they last typed, not what was last saved. A message being edited when the run
    // died is the one whose bubble is furthest behind the operator's intent, and handing
    // back the superseded text would quietly discard the edit the hold existed to protect.
    stash(
      leftovers
        .map(
          (m) =>
            (m.queuedMessageId && queuedDraft(m.queuedMessageId)) || m.content,
        )
        .join("\n"),
    );
    for (const m of leftovers)
      if (m.queuedMessageId) forgetQueuedDraft(m.queuedMessageId);
    toast.warn(
      "Your queued message wasn't delivered — it's back in the input.",
    );
  }

  /** Send while a run is live: the backend queues the message into that run
   *  (injected at its next boundary) — or, if the run ended in the meantime,
   *  starts a fresh turn; the response tells us which, so this client never
   *  picks. The optimistic bubble renders QUEUED until `message.injected`
   *  promotes it (or a withdraw/terminal removes it). */
  async function sendWhileStreaming(text: string): Promise<boolean> {
    const conversationId = deps.conversationId();
    if (conversationId === null) {
      // The first turn's POST hasn't resolved yet, so there's no conversation to
      // queue against. The composer already cleared itself — hand the text back
      // rather than dropping it.
      stash(text);
      // Handed back already, through the prefill — so this counts as taken: a false
      // here would have the composer restore the same text a second time.
      return true;
    }
    const userMsg: ChatMessage = {
      id: nextId("u"),
      role: "user",
      content: text.trim(),
      createdAt: new Date().toISOString(),
      queuedPending: true,
    };
    deps.setMessages(produce((m) => m.push(userMsg)));
    let created: ChatCreatedDTO;
    try {
      created = await api.post<ChatCreatedDTO>("/chat", {
        prompt: text.trim(),
        conversation_id: conversationId,
      });
    } catch (err) {
      // Not accepted (a regenerate/edit holds the claim, or transport failed):
      // roll the bubble back so the transcript only shows what the backend has.
      deps.setMessages(
        reconcile(deps.messages.filter((m) => m.id !== userMsg.id)),
      );
      toast.error(
        isApiError(err) && err.status === 409
          ? "Can't queue this message right now — try again in a moment."
          : ((err as { detail?: string })?.detail ?? "Unable to reach Talos."),
      );
      return false;
    }
    if (created.queued_message_id) {
      // Queued into the live run. The `message.queued` fold may have already
      // tagged the bubble via the open stream; this is the fallback tag.
      deps.patchById(userMsg.id, (m) => {
        if (!m.queuedMessageId) m.queuedMessageId = created.queued_message_id!;
      });
      return true;
    }
    // The run went terminal just before the POST landed: the backend started a
    // fresh run for this message instead. Promote the bubble to a normal turn
    // and drive the new run like any other send.
    deps.patchById(userMsg.id, (m) => (m.queuedPending = false));
    const assistantId = nextId("a");
    deps.setMessages(
      produce((m) =>
        m.push({
          id: assistantId,
          role: "assistant",
          model: deps.selection()?.model,
          content: "",
          blocks: [],
          streaming: true,
          queued: true,
          createdAt: new Date().toISOString(),
        }),
      ),
    );
    deps.setSending(true);
    deps.adoptConversationId(created.conversation_id);
    await deps.driveRun(created.run_id, assistantId);
    return true;
  }

  /** Withdraw a steering message that's still queued on the live run. No
   *  optimistic removal: a 404 means the run consumed it in the meantime — the
   *  message is part of the turn and its bubble must stay. On success the
   *  bubble drops here and the `message.withdrawn` fold is a no-op. */
  async function withdrawQueued(queuedMessageId: string): Promise<void> {
    const runId = deps.activeRunId();
    if (!runId) return;
    try {
      await api.del(`/runs/${runId}/messages/${queuedMessageId}`);
      deps.setMessages(
        reconcile(
          deps.messages.filter(
            (m) => !(m.queuedMessageId === queuedMessageId && m.queuedPending),
          ),
        ),
      );
    } catch (err) {
      if (isApiError(err) && err.status === 404) {
        toast.warn(
          "Too late to withdraw — the message already reached the model.",
        );
      } else {
        toast.error(
          (err as { detail?: string })?.detail ??
            "Unable to withdraw the message.",
        );
      }
    }
  }

  /** Rewrite a steering message that's still queued on the live run (it keeps
   *  its id and place in the queue). No optimistic update: the bubble changes
   *  only once the backend accepts, and a 404 means the run consumed the
   *  message in the meantime — the original text is what the model saw, so the
   *  bubble must keep it. */
  async function editQueued(
    queuedMessageId: string,
    text: string,
  ): Promise<void> {
    const runId = deps.activeRunId();
    if (!runId) return;
    try {
      await api.patch(`/runs/${runId}/messages/${queuedMessageId}`, { text });
      deps.setMessages(
        produce((list) => {
          const bubble = list.find(
            (m) => m.queuedMessageId === queuedMessageId && m.queuedPending,
          );
          if (bubble) bubble.content = text;
        }),
      );
    } catch (err) {
      if (isApiError(err) && err.status === 404) {
        toast.warn("Too late to edit — the message already reached the model.");
      } else {
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to edit the message.",
        );
      }
    }
  }

  /** Hold a queued message back, or release it.
   *
   *  It is the *run* that holds, not this client: the drain is server-side, so a message
   *  the operator is still writing would otherwise be injected out from under the editor
   *  and the edit lost. A failure is reported and nothing is assumed — a 404 means the
   *  run consumed it first, which is exactly the race the hold exists to close and the
   *  one case where it genuinely is too late. Releasing is best-effort and silent: it
   *  runs on teardown paths where a toast would be noise about a message that is gone. */
  async function holdQueued(
    queuedMessageId: string,
    held: boolean,
  ): Promise<void> {
    const runId = deps.activeRunId();
    if (!runId) return;
    try {
      await api.post(`/runs/${runId}/messages/${queuedMessageId}/hold`, {
        held,
      });
    } catch (err) {
      if (!held) return;
      if (isApiError(err) && err.status === 404)
        toast.warn("Too late to edit — the message already reached the model.");
      else
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to hold the message.",
        );
    }
  }

  return {
    undeliveredDraft,
    clearUndeliveredDraft: () => setUndeliveredDraft(null),
    stash,
    holdQueued,
    restoreUndelivered,
    sendWhileStreaming,
    withdrawQueued,
    editQueued,
  };
}
