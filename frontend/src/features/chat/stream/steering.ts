/**
 * The conversation's inbox, and the operator's hand on it.
 *
 * Every message sent while a turn is going lives in the conversation's inbox, which
 * outlives any run. This module owns the client's copy (`inbox.ts` holds the transitions)
 * and relays edit, hold, withdraw and resume to the backend.
 *
 * **No optimistic change on a message the model may already have read.** An edit or a
 * withdraw races the run's next boundary, and the backend answers 404 when it lost. The
 * item changes only *after* the backend accepts: what the model actually saw is what the
 * thread has to keep showing.
 *
 * **The operator's words are never dropped.** Text that has no turn to carry it (the
 * first turn's POST not yet resolved, or a recalled edit whose message was injected
 * first) goes through `stash`, and the composer prefills from it.
 */

import { createSignal, type Accessor } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { toast } from "~/ui";
import type { InboxDTO } from "../data/wire";
import {
  editItem,
  emptyInbox,
  holdItem,
  inboxFromDto,
  removeItem,
  type InboxState,
} from "../inbox";

export interface SteeringDeps {
  /** The thread the stream is bound to right now — null while it is still unsaved,
   *  which is the one case there is no inbox to read. */
  conversationId: () => string | null;
  /** The backend may have started a turn from the inbox (a resume, a withdraw or a
   *  release lets the queue move): go find it. `due` is whether something was released
   *  for certain, so a turn that already ran to its end still gets shown. */
  afterRelease: (due: boolean) => void;
}

export interface SteeringOps {
  inbox: Accessor<InboxState>;
  setInbox: (next: InboxState) => void;
  /** Read the inbox again. Needed whenever messages may have queued without an event:
   *  on load, after a send, when a run ends, after a resume. */
  refresh: () => Promise<void>;
  /** Read the inbox once whatever it is about to start has started, with the run that
   *  is live on the thread. Null when there is no thread or the read failed. */
  settle: () => Promise<InboxDTO | null>;
  /** Back to an empty inbox — the thread changed. */
  reset: () => void;
  withdraw: (messageId: string) => Promise<void>;
  edit: (messageId: string, text: string) => Promise<void>;
  /** Ask the inbox to hold a message back, or to stop holding it. Taken while the
   *  operator has it open in an editor, released when they save or give up. */
  hold: (messageId: string, held: boolean) => Promise<void>;
  /** Release a Stop's hold on the inbox without sending anything. */
  resume: () => Promise<void>;
  /** Text handed back to the composer by `stash`; the composer prefills from it, then
   *  clears it. */
  undeliveredDraft: Accessor<string | null>;
  clearUndeliveredDraft: () => void;
  /** Hand text back to the composer rather than dropping it. */
  stash: (text: string) => void;
}

export function createSteeringOps(deps: SteeringDeps): SteeringOps {
  const [inbox, setInbox] = createSignal<InboxState>(emptyInbox);
  const [undeliveredDraft, setUndeliveredDraft] = createSignal<string | null>(
    null,
  );

  const heldIn = new Map<string, string>();

  const stash = (text: string): void => {
    setUndeliveredDraft((prev) => (prev ? `${prev}\n${text}` : text));
  };

  /** Apply an inbox read, but only while the room is still on the thread it was for. */
  const adopt = (conversationId: string, dto: InboxDTO): void => {
    if (deps.conversationId() === conversationId) setInbox(inboxFromDto(dto));
  };

  async function refresh(): Promise<void> {
    const id = deps.conversationId();
    if (id === null) {
      setInbox(emptyInbox);
      return;
    }
    try {
      adopt(id, await api.get<InboxDTO>(`/conversations/${id}/inbox`));
    } catch {
      // A failed read leaves what the events built; the next one corrects it.
    }
  }

  async function settle(): Promise<InboxDTO | null> {
    const id = deps.conversationId();
    if (id === null) return null;
    try {
      const dto = await api.post<InboxDTO>(`/conversations/${id}/inbox/settle`);
      adopt(id, dto);
      return deps.conversationId() === id ? dto : null;
    } catch {
      return null;
    }
  }

  const tooLate = (what: string): void => {
    toast.warn(`Too late to ${what} — the message already reached the model.`);
  };

  async function withdraw(messageId: string): Promise<void> {
    const id = deps.conversationId();
    if (id === null) return;
    try {
      await api.del(`/conversations/${id}/inbox/${messageId}`);
      setInbox(removeItem(inbox(), messageId));
      if (id === deps.conversationId()) deps.afterRelease(false);
    } catch (err) {
      if (isApiError(err) && err.status === 404) {
        tooLate("withdraw");
        void refresh();
      } else {
        toast.error(
          (err as { detail?: string })?.detail ??
            "Unable to withdraw the message.",
        );
      }
    }
  }

  async function edit(messageId: string, text: string): Promise<void> {
    const id = deps.conversationId();
    if (id === null) return;
    try {
      await api.patch(`/conversations/${id}/inbox/${messageId}`, { text });
      setInbox(editItem(inbox(), messageId, text));
    } catch (err) {
      if (isApiError(err) && err.status === 404) {
        tooLate("edit");
        // What they typed has no message to land on any more.
        stash(text);
        void refresh();
      } else {
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to edit the message.",
        );
      }
    }
  }

  /** It is the *inbox* that holds, not this client: delivery is server-side, so a
   *  message the operator is still writing would otherwise go out from under the
   *  editor. Releasing is best-effort and silent: it runs on teardown paths where a
   *  toast would be noise about a message that is gone. */
  async function hold(messageId: string, held: boolean): Promise<void> {
    // A release is aimed at the thread that took the hold: an editor closed by a thread
    // switch would otherwise release against the thread just opened and leave the old
    // one held.
    const id = held
      ? deps.conversationId()
      : (heldIn.get(messageId) ?? deps.conversationId());
    if (id === null) return;
    if (held) heldIn.set(messageId, id);
    else heldIn.delete(messageId);
    try {
      await api.post(`/conversations/${id}/inbox/${messageId}/hold`, { held });
      if (id === deps.conversationId()) {
        setInbox(holdItem(inbox(), messageId, held));
        if (!held) deps.afterRelease(false);
      }
    } catch (err) {
      if (!held) return;
      if (isApiError(err) && err.status === 404) {
        tooLate("edit");
        void refresh();
      } else {
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to hold the message.",
        );
      }
    }
  }

  async function resume(): Promise<void> {
    const id = deps.conversationId();
    if (id === null) return;
    try {
      adopt(id, await api.post<InboxDTO>(`/conversations/${id}/inbox/resume`));
      if (id === deps.conversationId()) deps.afterRelease(true);
    } catch (err) {
      toast.error(
        (err as { detail?: string })?.detail ?? "Unable to resume the queue.",
      );
    }
  }

  return {
    inbox,
    setInbox,
    refresh,
    settle,
    reset: () => setInbox(emptyInbox),
    withdraw,
    edit,
    hold,
    resume,
    undeliveredDraft,
    clearUndeliveredDraft: () => setUndeliveredDraft(null),
    stash,
  };
}
