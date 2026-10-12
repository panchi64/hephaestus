/**
 * Reading the open thread's history, and noticing when it has gone stale.
 *
 * Split from `mainChat.ts` because it answers a different question from the rest of the
 * controller: not "what is the room bound to" but "what does the backend say this thread
 * holds, and is that still true". `createHistoryLoad` is the read and what is withheld
 * from the stream while it is not the open thread's own; `watchLateRuns` is the pair of
 * effects that re-read it when a run starts somewhere this room cannot hear.
 */

import {
  createEffect,
  createMemo,
  createSignal,
  on,
  onCleanup,
  untrack,
  type Accessor,
} from "solid-js";
import { useChatSession } from "./data/conversations";
import { refreshSessions, useChatSessions } from "./data/sessions";
import { historyStatus, type HistoryStatus } from "./historyStatus";
import type { ChatSession } from "./model";

/** How often an open, idle thread looks at the list for a run started out of sight. */
const IDLE_LIST_POLL_MS = 20_000;

export interface HistoryLoad {
  /** The loaded history, but only when it is the open thread's own. The resource still
   *  reports the previous thread's value across a source change (Solid retains it) and
   *  after a failed read has none to give, and feeding either to the stream would seed
   *  the wrong thread — or throw, since reading an errored resource re-throws. So this
   *  is the one place the stream's inputs are withheld. */
  loaded: () => ChatSession | undefined;
  /** A `loaded` field, withheld in lockstep with it. */
  fromLoaded: <T>(pick: (s: ChatSession) => T) => () => T | undefined;
  /** Whether the open thread's history is loading, failed, or here. */
  history: Accessor<HistoryStatus>;
  /** Read the open thread's history again. */
  reloadHistory: () => void;
}

export function createHistoryLoad(
  currentId: Accessor<string | null>,
): HistoryLoad {
  // Bumped to re-read the open thread's history: the same source, so it takes a
  // revision to make the resource ask again.
  const [reloads, setReloads] = createSignal(0);
  const session = useChatSession(currentId, reloads);
  const loaded = (): ChatSession | undefined => {
    if (session.state !== "ready") return undefined;
    const read = session();
    return read?.id === currentId() ? read : undefined;
  };
  const fromLoaded =
    <T>(pick: (s: ChatSession) => T) =>
    (): T | undefined => {
      const s = loaded();
      return s ? pick(s) : undefined;
    };
  const history = createMemo(() =>
    historyStatus({
      id: currentId(),
      state: session.state,
      holdsThisThread: loaded() !== undefined,
    }),
  );
  const reloadHistory = (): void => {
    setReloads((n) => n + 1);
  };
  return { loaded, fromLoaded, history, reloadHistory };
}

/** What the late-run watch reads off the room and the stream. */
export interface LateRunWatch {
  currentId: Accessor<string | null>;
  history: Accessor<HistoryStatus>;
  reloadHistory: () => void;
  sending: Accessor<boolean>;
  activeRunId: Accessor<string | null>;
}

/** Find a run that started out of sight. Call inside a reactive root. */
export function watchLateRuns(w: LateRunWatch): void {
  // A run started somewhere this room is not watching — another tab, a scheduled
  // task — has no stream here to hear it on, and the history read
  // at open said the thread was idle. The rail already knows: each row's activity
  // edge is server-derived. So when a poll reports the open thread as busy while this
  // room is attached to nothing, read the thread again; its `active_run` is what the
  // stream's cold-read reattach keys on. Keyed on the list alone (everything else is
  // read untracked) so one poll costs at most one read — tracking the history status
  // too would make each read trigger the next.
  const sessions = useChatSessions();
  createEffect(
    on(
      sessions,
      (list) => {
        const id = untrack(w.currentId);
        if (!id || !list) return;
        if (!list.find((s) => s.id === id)?.activity) return;
        if (untrack(w.history) !== "ready") return;
        if (untrack(w.sending) || untrack(w.activeRunId)) return;
        w.reloadHistory();
      },
      { defer: true },
    ),
  );
  // The rail only polls while it already shows something running, so a run that
  // starts out of sight would never light its row for the poll above to act on. While
  // a thread is open and idle, look at the list now and then — slowly, and only while
  // the tab is on screen.
  createEffect(() => {
    if (!w.currentId() || w.history() !== "ready" || w.sending()) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refreshSessions();
    }, IDLE_LIST_POLL_MS);
    onCleanup(() => clearInterval(timer));
  });
}
