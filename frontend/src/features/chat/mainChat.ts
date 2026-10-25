/**
 * The persistent main-chat controller.
 *
 * The chat room's stream, its selected conversation, and its loaded history live here —
 * under a never-disposed root — rather than inside the screen component. Navigating away
 * and back therefore no longer tears down an in-flight turn: a run started on one visit
 * keeps streaming into this store, and re-entering the room re-binds to it instead of
 * fetching an as-yet-unpersisted (empty) thread. (A turn's messages are only persisted
 * when it finishes, so a mid-stream refetch would otherwise read an empty conversation and
 * the room would render blank.)
 *
 * The compare panes still spin up their own throwaway `createChatStream`s — only the
 * single main room is this long-lived singleton.
 *
 * **What this module owns that the stream does not:** the *binding* the operator is
 * currently pointed at. Only the permission level, and only because unlike the project it
 * is sent on every turn and persisted per conversation, so it belongs to the thread on
 * screen rather than to the window.
 *
 * The project a new thread will be scoped to is deliberately **not** re-exposed here. It
 * is the app-wide store's (`lib/stores/stagedProject`), and aliasing it onto this handle
 * would make a surface that only wanted to read a plain signal instantiate the whole
 * never-disposed stream singleton, its SSE effects and its visibility listeners.
 */

import {
  createEffect,
  createRoot,
  createSignal,
  on,
  onCleanup,
  untrack,
  type Accessor,
} from "solid-js";
import {
  setAwaitingInput,
  setChatBusy,
  setChatThreadId,
  setRunErrored,
} from "~/lib/stores/chatActivity";
import { markConversationRead } from "~/lib/stores/notifications";
import { moveDraft } from "~/ui";
import { agentToSend } from "~/features/agents/model";
import { stagedProjectId } from "~/lib/stores/stagedProject";
import { refreshSessions } from "./data/sessions";
import { createHistoryLoad, watchLateRuns } from "./historyLoad";
import type { HistoryStatus } from "./historyStatus";
import { DEFAULT_PERMISSION_LEVEL, type PermissionLevel } from "./model";
import { seatPermission } from "./permissionSeat";
import { createReadOnView } from "./readOnView";
import { createChatStream } from "./stream/chatStream";

export interface MainChat {
  currentId: Accessor<string | null>;
  /** Seat the room on a thread. Re-selecting the thread already open re-reads its
   *  history instead of doing nothing — which is what makes a failed load recoverable
   *  by clicking the row again. */
  setCurrentId: (id: string | null) => void;
  /** Whether the open thread's history is loading, failed, or here. The transcript
   *  draws a loading line or a retryable error from it rather than the empty state. */
  history: Accessor<HistoryStatus>;
  /** Read the open thread's history again. */
  reloadHistory: () => void;
  /** The thread **on screen**, which is `currentId` except during the first turn of a
   *  thread the operator has just started: the backend gives that one an id the moment
   *  it accepts the turn, but the room does not seat itself on it until the run ends
   *  (seating re-reads history, re-keys the composer's draft, and re-seeds the panel —
   *  none of which is safe to do underneath a live turn).
   *
   *  Everything that merely *names or marks* that thread should read this instead:
   *  keyed on `currentId`, the header had no id to look a title up against, so an
   *  auto-generated name — which the backend emits within a second or two of the turn
   *  starting — could not render until the stream was over. Anything that *acts on*
   *  the thread still uses `currentId`; this is a read-side answer to "which thread am
   *  I looking at", not a second seat. */
  openId: Accessor<string | null>;
  stream: ReturnType<typeof createChatStream>;
  /** Rises each time the stream reports the agent starting a browser. The stream
   *  outlives the room, so the room's browser state reacts to this rather than being
   *  handed a callback. */
  browserOpenedSeq: Accessor<number>;
  /** Whether the one-time warm-resume entry intent has run this app session. The
   *  flag is part of the singleton so it survives navigation — see the screen. */
  warmResolved: Accessor<boolean>;
  markWarmResolved: () => void;
  /** How far the model may go in the open thread. Sent on every turn and persisted
   *  per conversation, so the composer's control moves it mid-thread; seeded from the
   *  loaded thread so a reload comes back where the operator left it, and reset to the
   *  default when a new thread is staged. */
  permission: Accessor<PermissionLevel>;
  setPermission: (level: PermissionLevel) => void;
  /** Whether the level above is a stand-in for one still loading (`seatPermission`'s
   *  `provisional`). The composer renders the control as pending on it rather than
   *  asserting the placeholder as the thread's level. */
  permissionPending: Accessor<boolean>;
  /** The agent the operator picked for the thread on screen and has not yet had
   *  confirmed by a send — undefined until they pick one. The composer shows this
   *  over the thread's stored agent, and the next send names it. */
  agentChoice: Accessor<string | undefined>;
  setAgentChoice: (name: string) => void;
}

/** The key the room's composer keeps a thread's unsent draft under — `null` is the
 *  staged, not-yet-created thread. One derivation for the room that reads it and the
 *  adoption below that moves it, so the two can't key the same thread differently. */
export const chatDraftKey = (id: string | null): string =>
  `chat:${id ?? "new"}`;

let _mainChat: MainChat | undefined;

/** The app-wide chat room controller — created once, then reused across mounts. */
export function mainChat(): MainChat {
  if (_mainChat) return _mainChat;
  return (_mainChat = createRoot(() => {
    const [currentId, setCurrentId] = createSignal<string | null>(null);
    const [permission, setLevel] = createSignal<PermissionLevel>(
      DEFAULT_PERMISSION_LEVEL,
    );
    // The thread the level above was chosen for. Plain, not a signal: nothing renders
    // it, and it exists only so `seatPermission` can tell "the operator's choice for
    // the thread on screen" from "the last thread's level, still sitting there".
    let permissionOwner: string | null = null;
    const [browserOpenedSeq, setBrowserOpenedSeq] = createSignal(0);
    // Whether the seated level is the strictest-level stand-in for one still in flight.
    // Only the seating effect writes it; an operator's own choice is never provisional,
    // and it cannot be made while the control is pending on this flag.
    const [permissionPending, setPermissionPending] = createSignal(false);
    const setPermission = (level: PermissionLevel): void => {
      permissionOwner = currentId();
      setLevel(level);
    };
    const [agentChoice, setChoice] = createSignal<string | undefined>(
      undefined,
    );
    const [agentSent, setAgentSent] = createSignal<string | undefined>(
      undefined,
    );
    // The thread the choice above was made for, so a switch of thread drops it while
    // the adoption of a just-created one does not.
    let agentOwner: string | null = null;
    const setAgentChoice = (name: string): void => {
      agentOwner = currentId();
      setChoice(name);
    };
    const { loaded, fromLoaded, history, reloadHistory } =
      createHistoryLoad(currentId);
    const stream = createChatStream(
      fromLoaded((s) => s.messages),
      currentId,
      {
        onConversationStarted: (id) => {
          // The staged thread has just been given its backend id. It is the same
          // thread, so the level the operator picked for it moves across with it —
          // otherwise the seating rule below would read the new id as a *different*
          // thread whose level is unknown and drop the control to the default,
          // mid-turn, on the very thread that was created with it.
          permissionOwner = id;
          agentOwner = id;
          // So does whatever the operator typed while the first turn ran. The
          // composer's draft is keyed by thread, and the seat below re-keys it: left
          // under the staged key, that text would vanish from the field the moment the
          // run ended and resurface on the *next* new thread. Moved first, so the key
          // the composer flips to already holds it — and here rather than in the room,
          // because this is the one place that knows the flip is an adoption and not a
          // navigation, and it still happens with the room unmounted.
          moveDraft(chatDraftKey(null), chatDraftKey(id));
          setCurrentId(id);
        },
        onTurnComplete: () => refreshSessions(),
        // Withheld in lockstep with the history above, so the meter seeds from the
        // loaded thread rather than the retained value of the one just left.
        initialContext: fromLoaded((s) => s.context),
        // Same lockstep: the loaded thread's cumulative readout.
        initialStats: fromLoaded((s) => s.stats),
        // Same lockstep: the in-flight run of the loaded thread, for a cold-read
        // reattach (a page reload mid-stream).
        activeRun: fromLoaded((s) => s.activeRun),
        untitled: fromLoaded((s) => s.untitled),
        // Same lockstep: the loaded thread's git-style snapshot history.
        initialSnapshots: fromLoaded((s) => s.snapshots),
        // Read only when a send creates the conversation.
        projectId: stagedProjectId,
        // Read on every send — the level is the one binding fact that moves.
        permission,
        // Named only when the operator changed it and a send has not yet carried it.
        agent: (isNewThread) =>
          agentToSend({
            choice: agentChoice(),
            sent: agentSent(),
            isNewThread,
          }),
        onAgentSent: setAgentSent,
        // ...and it can now move from the *other* end too: `plan_enter` narrows it and
        // an approved plan raises it, both from inside the run. Seating it here is what
        // stops the next send from writing the level the control was still holding back
        // over the one the thread is actually at. Never provisional — this is the thread
        // saying so, not a placeholder for a value still in flight.
        onBrowserOpened: () => setBrowserOpenedSeq((n) => n + 1),
        onPermissionChanged: (level) => {
          setPermission(level);
          setPermissionPending(false);
        },
      },
    );
    createEffect(() => {
      const id = currentId();
      if (id === agentOwner) return;
      agentOwner = id;
      setChoice(undefined);
      setAgentSent(undefined);
    });
    // Opening a thread seats the composer's level control from what the thread
    // stored, rather than leaving it on the previous thread's value.
    //
    // The level is re-seated on *every* pass, not only once the load lands: the gap
    // between the click and the fetch is exactly when a send would carry the previous
    // thread's level onto this one. `seatPermission` owns that rule.
    createEffect(() => {
      const id = currentId();
      // Withheld across a source change in lockstep with everything else fed to the
      // stream — the resource retains the thread just left, and its level is the one
      // value here that would be written back to the wrong conversation.
      const thread = loaded();
      const seat = seatPermission({
        currentId: id,
        owner: permissionOwner,
        stored: thread?.permission,
      });
      if (seat) {
        permissionOwner = seat.owner;
        setLevel(seat.level);
        setPermissionPending(seat.provisional);
      }
    });
    // Reattach when the tab returns to the foreground or the network comes back.
    // A backgrounded tab throttles the SSE reader until it stalls; on return we
    // replay from the last folded seq — resuming a still-live run or folding the
    // tail of one that finished while we were away (which clears the frozen
    // streaming state via the drive's finally). No-op when nothing is in flight.
    const resume = () => {
      const runId = stream.activeRunId();
      const inFlight =
        stream.sending() || stream.messages.some((m) => m.streaming);
      if (runId && inFlight) {
        void stream.reattachRun(runId, { fromSeq: stream.lastSeq() });
      }
    };
    const [pageVisible, setPageVisible] = createSignal(
      document.visibilityState === "visible",
    );
    const onVisible = () => {
      setPageVisible(document.visibilityState === "visible");
      if (pageVisible()) resume();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", resume);
    onCleanup(() => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", resume);
    });
    // Mirror the main room's streaming state to the global flag the nav rail reads,
    // so the Chat item shows a live indicator while a turn runs — even from another
    // section. Only the main room drives it (compare panes are ephemeral).
    createEffect(() => setChatBusy(stream.sending()));
    createEffect(() => setChatThreadId(currentId()));
    // A turn *starting* changes the list as much as one finishing: each row's
    // activity edge is server-derived, so the list has to be re-read for the
    // running thread to light up. `onTurnComplete` above covers the other edge.
    createEffect(
      on(
        () => stream.sending(),
        (sending) => {
          if (sending) refreshSessions();
        },
        { defer: true },
      ),
    );
    // Same main-room-only mirror for the last-run-error echo, so the favicon can flag a
    // failed run from any screen.
    createEffect(() => setRunErrored(stream.errored()));
    // Same mirror for the awaiting-approval echo, so the nav rail and favicon can flag
    // a parked run needing a decision from any screen, not just while on /chat.
    createEffect(() => setAwaitingInput(stream.awaitingInput()));
    // The one place a thread selection lands, whether from the RECENTS rail, the chat
    // screen's warm-resume or a notification's deep-link, so every caller gets it.
    const openId = () => currentId() ?? stream.conversationId();
    const readOnView = createReadOnView(markConversationRead);
    createEffect(() => readOnView(openId(), pageVisible()));
    // Runs begun out of sight (another tab, a scheduled task) are
    // found by re-reading the open thread — see `watchLateRuns`.
    watchLateRuns({
      currentId,
      history,
      reloadHistory,
      sending: stream.sending,
      activeRunId: stream.activeRunId,
    });
    const [warmResolved, setWarmResolved] = createSignal(false);
    return {
      currentId,
      browserOpenedSeq,
      setCurrentId: (id) => {
        if (
          id !== null &&
          id === untrack(currentId) &&
          !untrack(stream.sending)
        )
          reloadHistory();
        else setCurrentId(id);
      },
      history,
      reloadHistory,
      openId,
      stream,
      warmResolved,
      markWarmResolved: () => setWarmResolved(true),
      permission,
      setPermission,
      permissionPending,
      agentChoice,
      setAgentChoice,
    } satisfies MainChat;
  }));
}
