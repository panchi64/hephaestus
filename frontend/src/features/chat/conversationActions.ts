/**
 * What the operator can do to the *thread*, as opposed to a turn inside it: retitle, fork,
 * copy, open the thread's browser, and the two deletes.
 *
 * The exported `retitleConversation` / `deleteConversationFlow` take an id, so the rail's
 * per-row menu can act on a thread that isn't open. `createConversationActions` wraps them
 * for the room, adding what is about the room: the retitle throbber and restaging the
 * composer once the open thread is gone.
 *
 * A delete also removes the image uploads it leaves unreferenced; the backend decides
 * which, since an image may still be attached elsewhere.
 *
 * A thread with a live run is not deleted: aborting the local SSE does not stop the run
 * server-side. The menus disable the row ("Stop the turn first") instead.
 */

import { createSignal, type Accessor } from "solid-js";
import { isApiError } from "~/lib/api";
import { confirm, copyToClipboard, toast } from "~/ui";
import { buildConversationTranscript } from "./transcriptExport";
import {
  deleteConversation,
  forkConversation,
  openBrowser,
  regenerateTitle,
} from "./data";
import type { BrowserState } from "./browserState";
import type { ChatMessage } from "./model";

function confirmDelete(title: string, detail: string): Promise<boolean> {
  return confirm({ title, detail, confirmLabel: "Delete", tone: "alert" });
}

/** The threads whose titles the backend is naming right now. Keyed by thread rather than
 *  held by the room, so the throbber stays on the thread being named whichever thread is
 *  open, and module-level because a retitle outlives the menu that started it. */
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
 *  before. Callers keep the row disabled while the thread has a live run. */
export async function deleteConversationFlow(
  conversationId: string,
): Promise<boolean> {
  const ok = await confirmDelete(
    "Delete this conversation?",
    "This permanently removes the thread, its history and its images.",
  );
  if (!ok) return false;
  try {
    await deleteConversation(conversationId);
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
  /** The name the export gives the agent's turns. */
  speaker: () => string;
  /** Whether a turn is in flight, which switches off every act that rewrites or
   *  removes the thread. */
  sending: () => boolean;
  removeMessage: (messageId: string) => Promise<void>;
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
    if (!id || deps.sending()) return;
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
    if (!id || deps.sending()) return;
    if (await deleteConversationFlow(id)) deps.onDeleted();
  }

  async function removeMessage(messageId: string): Promise<void> {
    const id = deps.conversationId();
    if (!id || deps.sending()) return;
    const ok = await confirmDelete(
      "Delete this message?",
      "This removes it, everything after it, and their images.",
    );
    if (!ok) return;
    await deps.removeMessage(messageId);
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
        buildConversationTranscript(deps.messages, deps.speaker()),
        "Conversation",
      ),
  };
}
