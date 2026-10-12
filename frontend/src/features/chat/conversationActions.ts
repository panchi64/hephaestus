/**
 * What the operator can do to the *thread* — as opposed to a turn inside it.
 *
 * Rename lives in its own modal, but the rest of the session menu ends up here: retitle,
 * fork, copy, open the thread's browser, and the two deletes. Each is a confirm-or-probe,
 * a call, and a toast, and none of them belongs in the screen that lays out the
 * conversation — the screen was carrying a third of its length in error handling for
 * actions it does not render.
 *
 * **Two layers, and the split is which conversation is being acted on.** The exported
 * `retitleConversation` / `deleteConversationFlow` take an id, so the rail's per-row
 * menu can use them on a thread that isn't open. `createConversationActions` wraps them
 * for the room, adding only what is genuinely about the room: the retitle throbber,
 * stopping a live run, restaging the composer once the open thread is gone.
 *
 * **A delete has to ask a second question first.** Images are shared: the same attachment
 * can be referenced from more than one place, so deleting the thread that happens to hold
 * one may strand it or may not. The backend is asked which images *this* delete would
 * orphan, and only when there are any does the operator get the three-way keep/purge
 * prompt — a plain confirm otherwise. A failed probe falls back to the plain confirm and
 * keeps the images: an unreachable answer is not a licence to delete more.
 *
 * **Stop the run before removing what it is writing into.** Aborting the local SSE does
 * not stop the run server-side, so a thread deleted mid-turn would keep generating into a
 * conversation that no longer exists.
 */

import { createSignal, type Accessor } from "solid-js";
import { isApiError } from "~/lib/api";
import { confirm, confirmChoice, copyToClipboard, toast } from "~/ui";
import { assembleTranscript } from "./blocks";
import {
  deleteConversation,
  fetchOrphanImageAttachments,
  forkConversation,
  openBrowser,
  regenerateTitle,
} from "./data";
import type { BrowserState } from "./browserState";
import type { ChatMessage } from "./model";

/** Flatten the whole thread to plain text for COPY CONVERSATION — each turn's
 *  role and content in order, assistant turns including their tool calls/
 *  results via `assembleTranscript` (same shaping as per-message COPY
 *  MESSAGE), separated by rules so the export reads as a transcript. */
export function buildConversationTranscript(messages: ChatMessage[]): string {
  return messages
    .map((m) => {
      // A compaction divider is neither party's words — label it as the chassis note it
      // is rather than attributing the summary to the assistant.
      const label =
        m.role === "user"
          ? "Operator"
          : m.role === "compaction"
            ? "Context compacted"
            : "Assistant";
      const body =
        m.role === "assistant" ? assembleTranscript(m.blocks) : m.content;
      return `${label} · ${m.createdAt}\n${body}`;
    })
    .join("\n\n---\n\n");
}

/** Gate a delete that may strand image attachments. Probes the backend for the
 *  images this delete would orphan; if any, raises the 3-way keep/purge prompt,
 *  otherwise the plain confirm. Returns the chosen `purgeImages`, or null to
 *  abort. A failed probe falls back to the plain confirm (keep images) so the
 *  delete stays usable. */
async function resolveDeleteChoice(
  conversationId: string,
  title: string,
  baseDetail: string,
  messageId?: string,
): Promise<boolean | null> {
  let orphans: string[] = [];
  try {
    orphans = await fetchOrphanImageAttachments(conversationId, messageId);
  } catch {
    // Probe failed — fall through to the plain confirm below.
  }
  if (orphans.length === 0) {
    const ok = await confirm({
      title,
      detail: baseDetail,
      confirmLabel: "Delete",
      tone: "alert",
    });
    return ok ? false : null;
  }
  const n = orphans.length;
  const choice = await confirmChoice({
    title,
    detail: `${baseDetail} ${n} image attachment${
      n > 1 ? "s" : ""
    } would be left unused — delete them too, or keep them?`,
    confirmLabel: "Delete images",
    secondaryLabel: "Keep images",
    cancelLabel: "Cancel",
    tone: "alert",
  });
  if (choice === "cancel") return null;
  return choice === "primary";
}

/* ── Acting on a thread by id ─────────────────────────────────────────────────
 *
 * The two below take the conversation they act on as an argument, because two
 * surfaces now reach for them: the open thread's own header, and any row in the
 * rail's context menu. They used to read the open thread's id from the room's
 * closure, which is exactly what made them unusable from a list — acting on a row
 * meant opening it first.
 *
 * `createConversationActions` still wraps them, and keeps everything that is
 * genuinely about *the room*: the retitle throbber, stopping a live run, restaging
 * an empty composer. Nothing of that belongs to a row in a list. */

/** The threads whose titles the backend is naming right now, keyed by id.
 *
 *  **A throbber belongs to the thread, not to the surface showing it.** It used to be a
 *  boolean the room held, so regenerating a title and then opening some other thread
 *  moved the spinner onto *that* thread's name — the room only knew "a retitle is in
 *  flight", never which conversation it was for. Retitling is also startable from a rail
 *  row for a thread that isn't open, which a room-scoped flag cannot represent at all.
 *
 *  Module-level, like the other app-lifetime stores: there is one operator, and a
 *  retitle outlives the menu that started it. */
const [retitlingIds, setRetitlingIds] = createSignal<ReadonlySet<string>>(
  new Set(),
);

function markRetitling(conversationId: string, active: boolean): void {
  setRetitlingIds((prev) => {
    const next = new Set(prev);
    if (active) next.add(conversationId);
    else next.delete(conversationId);
    return next;
  });
}

/** True while *this* thread is being named. Null (a staged thread) is never naming. */
export function isRetitling(conversationId: string | null): boolean {
  return conversationId !== null && retitlingIds().has(conversationId);
}

/** Regenerate a thread's title. Resolves once the backend has answered; any surface
 *  showing that thread reads `isRetitling(id)` for the in-flight indicator. */
export async function retitleConversation(
  conversationId: string,
): Promise<void> {
  if (isRetitling(conversationId)) return;
  markRetitling(conversationId, true);
  try {
    await regenerateTitle(conversationId);
    toast.success("Title regenerated");
  } catch {
    toast.error("Unable to regenerate the title.");
  } finally {
    markRetitling(conversationId, false);
  }
}

/** Confirm and delete a thread. Resolves true only when it is actually gone, so a
 *  caller can decide what to reseat *after* the operator has agreed rather than
 *  before.
 *
 *  `beforeDelete` runs between the confirm and the call, for a caller that has to
 *  settle something first — the room stops a live run there, because aborting the
 *  local SSE does not stop the run server-side and a thread deleted mid-turn would
 *  keep generating into a conversation that no longer exists. */
export async function deleteConversationFlow(
  conversationId: string,
  opts: { beforeDelete?: () => Promise<void> } = {},
): Promise<boolean> {
  const purgeImages = await resolveDeleteChoice(
    conversationId,
    "Delete this conversation?",
    "This permanently removes the thread and its history.",
  );
  if (purgeImages === null) return false;
  try {
    await opts.beforeDelete?.();
    await deleteConversation(conversationId, purgeImages);
    toast.success("Conversation deleted");
    return true;
  } catch {
    toast.error("Unable to delete the conversation.");
    return false;
  }
}

export interface ConversationActionDeps {
  /** The open thread, or null when the composer is staging a new one. */
  conversationId: () => string | null;
  /** The transcript, for the clipboard export. */
  messages: ChatMessage[];
  /** Whether a turn is in flight — a delete has to stop it first. */
  sending: () => boolean;
  cancel: () => Promise<void>;
  removeMessage: (messageId: string, purgeImages: boolean) => Promise<void>;
  /** The thread's browser, which an open marks as running. */
  browser: BrowserState;
  /** Put the browser panel on screen. */
  showBrowser: () => void;
  /** The open thread is gone; the room stages a new one. */
  onDeleted: () => void;
  /** The fork's id — the room opens it, leaving the source untouched. */
  onForked: (conversationId: string) => void;
}

export interface ConversationActions {
  /** True while *the open thread's* title is being regenerated — the header's
   *  throbber. Scoped to the conversation, so leaving mid-retitle takes the throbber
   *  with it rather than stamping it on whatever thread is opened next. */
  retitling: Accessor<boolean>;
  retitle: () => Promise<void>;
  fork: (messageId: string) => Promise<void>;
  /** Show this thread's agent browser in the panel, starting it first if it is not
   *  running. */
  openBrowser: () => Promise<void>;
  removeConversation: () => Promise<void>;
  removeMessage: (messageId: string) => Promise<void>;
  copyTranscript: () => void;
}

export function createConversationActions(
  deps: ConversationActionDeps,
): ConversationActions {
  const retitling = () => isRetitling(deps.conversationId());

  async function retitle(): Promise<void> {
    const id = deps.conversationId();
    if (!id) return;
    // The registry owns the in-flight guard as well as the indicator — a second press
    // from the rail's menu, on the same thread, must be the same no-op as a second
    // press from this menu.
    await retitleConversation(id);
  }

  /** Open a new conversation carrying history up to this turn.
   *
   *  The backend returns the *fork's* detail, so the room reseats onto the new id
   *  rather than reloading the source and hunting for it. The source thread is
   *  untouched, which is the whole point — a tangent shouldn't cost the
   *  conversation it came from. */
  async function fork(messageId: string): Promise<void> {
    const id = deps.conversationId();
    if (!id) return;
    try {
      deps.onForked(await forkConversation(id, messageId));
      toast.success("Forked into a new conversation.");
    } catch (err) {
      toast.error(
        isApiError(err) ? err.detail : "Unable to fork this conversation.",
      );
    }
  }

  /** Show the thread's browser in the panel — the headless Chromium the agent drives,
   *  watched and steered by the operator from here, so they can log in or land on a page
   *  before handing the thread on.
   *
   *  A session that is already running is only shown; nothing is posted. No success
   *  toast: the panel arriving is the confirmation. Only a failure has anything to say,
   *  and the backend's own sentence says it better than a generic line would. */
  async function open(): Promise<void> {
    const id = deps.conversationId();
    if (!id) return;
    if (!deps.browser.active()) {
      try {
        await openBrowser(id);
      } catch (err) {
        toast.error(
          isApiError(err) ? err.detail : "Unable to open the browser.",
        );
        return;
      }
      deps.browser.opened();
    }
    deps.showBrowser();
  }

  async function removeConversation(): Promise<void> {
    const id = deps.conversationId();
    if (!id) return;
    const deleted = await deleteConversationFlow(id, {
      // Deleting a thread mid-stream must stop its generation: cancel the live
      // run first (while it still exists) so the backend halts it, rather than
      // leaving it generating into a conversation that's about to be gone —
      // aborting the local SSE alone wouldn't stop the run server-side.
      //
      // `sending()` alone used to gate this, which was wrong the moment anything
      // but the open thread could be deleted: it would cancel the operator's live
      // run to delete some *other* conversation. The run only has to stop when the
      // thread being removed is the one writing into it.
      beforeDelete: async () => {
        if (id === deps.conversationId() && deps.sending()) await deps.cancel();
      },
    });
    if (deleted) deps.onDeleted();
  }

  async function removeMessage(messageId: string): Promise<void> {
    const id = deps.conversationId();
    if (!id) return;
    const purgeImages = await resolveDeleteChoice(
      id,
      "Delete this message?",
      "This removes it and everything after it.",
      messageId,
    );
    if (purgeImages === null) return;
    await deps.removeMessage(messageId, purgeImages);
    toast.success("Message deleted");
  }

  return {
    retitling,
    retitle,
    fork,
    openBrowser: open,
    removeConversation,
    removeMessage,
    copyTranscript: () =>
      copyToClipboard(
        buildConversationTranscript(deps.messages),
        "Conversation",
      ),
  };
}
