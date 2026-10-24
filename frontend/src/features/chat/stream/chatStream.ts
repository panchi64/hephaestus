/**
 * The thread controller: one conversation's live signals and the ops that start and stop
 * a turn, wiring together driving a run's transport (`drive.ts`), folding frames
 * (`fold.ts`), the queued-message inbox (`steering.ts`), parked-call decisions
 * (`approvals.ts`), reconciling with persisted rows (`resume.ts`), edits to recorded
 * turns (`branching.ts`) and thread switching (`binding.ts`).
 *
 * The store is authoritative for the live thread, the backend for what a turn was. The
 * prompt is persisted when a turn begins but the answer only when it finishes, so a
 * mid-stream refetch lacks the reply: `binding.ts` refuses to re-seed a live thread, and
 * `resume.ts` reseats from the backend's rows once the run ends.
 *
 * Teardown reconciles the thread and reconciling can start a drive, so two wirings reach
 * the drive at call time rather than holding its handle.
 */

import { batch, createEffect, createSignal, on, untrack } from "solid-js";
import { createStore, produce, reconcile } from "solid-js/store";
import { api } from "~/lib/api";
import { effectiveSelection, type ModelSelection } from "~/lib/stores/models";
import type { TaskItem } from "~/lib/stream";
import type { PlanDocument } from "../model";
import { toast } from "~/ui";
import { CONTINUE_PROMPT } from "../data/constants";
import { collectLateTitle } from "../data/conversations";
import { refreshSessions } from "../data/sessions";
import type { ChatSendDTO, ConversationDetailDTO } from "../data/wire";
import type {
  ActiveRun,
  ChatMessage,
  ContextUsage,
  ConversationStats,
  PermissionLevel,
  ViewSnapshotRef,
} from "../model";
import { createApprovalOps } from "./approvals";
import { createThreadBinding } from "./binding";
import { toMessage } from "../data/messages";
import {
  createBranchingOps,
  reseatFromDetail,
  type TranscriptStore,
} from "./branching";
import { askRestore } from "../askRestore";
import { adoptLiveIdentity } from "./reseatIdentity";
import { attachOptions, createRunDrive } from "./drive";
import {
  createFolder,
  type FoldState,
  type RunClock,
  type RunQueue,
} from "./fold";
import { createPatchById, nextId } from "./patch";
import { createResumeOps } from "./resume";
import { createSteeringOps } from "./steering";
import { deliveryDue } from "../inbox";

export interface ChatStreamOptions {
  /** Fired once when a brand-new conversation receives its backend id. */
  onConversationStarted?: (id: string) => void;
  /** Fired when a turn finishes (done or errored) — refresh the session list. */
  onTurnComplete?: () => void;
  /** Per-instance model override instead of the global picker (compare panes). */
  selection?: () => ModelSelection | null;
  /** Mark a new conversation as scratch, hidden from the sidebar (compare panes). */
  ephemeral?: boolean;
  /** Project for the next new conversation; read only on the send that creates it. */
  projectId?: () => string | undefined;
  /** Sent on every send, unlike the project: the level is a live control the backend
   *  persists from whichever send named it last. */
  permission?: () => PermissionLevel;
  /** The custom agent to name in this send; asked with whether the send creates the
   *  thread. Omitted leaves the thread's agent as it is. */
  agent?: (isNewThread: boolean) => string | undefined;
  /** Fired once a send that named an agent was accepted. */
  onAgentSent?: (name: string) => void;
  /** Fired when the run moved the level (`plan_enter` narrowing, an approved plan
   *  raising). Required: the level is sent on every message, so a caller that missed
   *  this would overwrite the change on the next send. */
  onPermissionChanged?: (level: PermissionLevel) => void;
  /** Fired when the run reports the agent attaching a browser to the thread. */
  onBrowserOpened?: () => void;
  /** Context-window state seeded with history so the meter shows before the next turn. */
  initialContext?: () => ContextUsage | null | undefined;
  /** Cumulative readout seeded the same way; rebuilt by the backend from stored messages. */
  initialStats?: () => ConversationStats | null | undefined;
  /** The in-flight run of a loaded conversation; seeds a reattach on a cold read. */
  activeRun?: () => ActiveRun | null | undefined;
  /** Whether the loaded conversation has no stored title yet: a reattached run on it
   *  may be the first turn, whose auto-title can land after the run ends. */
  untitled?: () => boolean | undefined;
  /** Workspace snapshots seeded with the messages. */
  initialSnapshots?: () => ViewSnapshotRef[] | undefined;
}

export function createChatStream(
  initial: () => ChatMessage[] | undefined,
  key: () => string | null = () => null,
  options: ChatStreamOptions = {},
) {
  const [messages, setMessages] = createStore<ChatMessage[]>([]);
  // Conversation-scoped, so they sit beside the messages rather than on message blocks.
  const [snapshots, setSnapshots] = createSignal<ViewSnapshotRef[]>([]);
  const [sending, setSending] = createSignal(false);
  // True when the last run ended in `run.error`; the main room mirrors it to the global
  // `runErrored` echo for the favicon, compare panes keep it local.
  const [errored, setErrored] = createSignal(false);
  // Drives the title throbber from a new thread's first turn until `conversation.titled`
  // lands, or until `awaitLateTitle` in the drive reports naming finished.
  const [titlePending, setTitlePending] = createSignal(false);
  // Null until a run reports it against a known window (loaded history carries none).
  const [usage, setUsage] = createSignal<ContextUsage | null>(null);
  // One signal: the live `run.metrics` frame and the conversation load's `stats` are the
  // same payload, so a reload continues the same numbers.
  const [stats, setStats] = createSignal<ConversationStats | null>(null);
  // Conversation-level, not a message block: one list is rewritten in place, and
  // `tasks.updated` carries the whole list, so applying it is a replace, never a merge.
  const [tasks, setTasks] = createSignal<TaskItem[]>([]);
  // One plan per thread, replaced on every revision.
  const [plan, setPlan] = createSignal<PlanDocument | null>(null);
  const [runClock, setRunClock] = createSignal<RunClock | null>(null);
  const [runQueue, setRunQueue] = createSignal<RunQueue | null>(null);
  // Run-scoped bookkeeping the fold advances and the drive resets, shared by reference
  // with the folder.
  const foldState: FoldState = {
    maxFoldedSeq: 0,
    foldTarget: null,
    tasksRevision: 0,
    planRevision: 0,
    activeRunId: null,
    runKind: null,
    epoch: 0,
    replayThrough: 0,
  };
  // Makes the cold-read reattach effect fire at most once per run.
  let reattachedRunId: string | null = null;
  // The bound conversation, held twice and written together: a plain `let` for
  // synchronous reads mid-callback (a signal read would subscribe the running effect),
  // and a signal so the screen can render against a new thread's id as soon as
  // `POST /chat` answers, before `key()` catches up.
  let activeConversationId: string | null = key();
  const [boundConversationId, setBoundConversationId] = createSignal<
    string | null
  >(activeConversationId);
  /** All writes to `activeConversationId` go through here, or the signal goes stale. */
  const bindConversation = (id: string | null): void => {
    activeConversationId = id;
    setBoundConversationId(id);
  };

  const store: TranscriptStore = { setMessages, setUsage, setSnapshots };
  const reseat = (detail: ConversationDetailDTO) =>
    reseatFromDetail(store, detail);
  // The finished turn takes the backend's ids in place first, so reconciling updates
  // it rather than remounting it.
  const reseatMessages = (detail: ConversationDetailDTO) => {
    const next = detail.messages.map(toMessage);
    const renames = adoptLiveIdentity(messages, next);
    batch(() => {
      if (renames.length)
        setMessages(
          produce((m) => {
            for (const [i, id] of renames) m[i].id = id;
          }),
        );
      setMessages(reconcile(next));
    });
  };

  const patchById = createPatchById(messages, setMessages);

  // Declared before the fold, which keeps the inbox current from `message.*` events.
  const steering = createSteeringOps({
    conversationId: () => activeConversationId,
    // Reached at call time, like the resume ops it goes through.
    afterRelease: (due) => void followQueued(due),
  });

  const foldEvent = createFolder({
    state: foldState,
    patchById,
    setMessages,
    inbox: { get: steering.inbox, set: steering.setInbox },
    setSnapshots,
    setTasks,
    setPlan,
    // See `FoldDeps.setPermission`; a no-op for callers with no level (compare pane).
    setPermission: (level) => options.onPermissionChanged?.(level),
    setUsage,
    setStats,
    setErrored,
    setTitlePending,
    setRunClock,
    setRunQueue,
    // A checkpoint names a turn this transcript lacks, so the persisted thread wins.
    // Reached at call time: the resume ops are built below.
    onFoldMisplaced: () => void resume.reconcileStaleDecision(),
    onBrowserOpened: options.onBrowserOpened,
  });

  // The inbox belongs to the thread, not a run: messages queued while no run is live
  // emit no events, so read it on every thread change.
  createEffect(
    on(boundConversationId, (id) => {
      steering.reset();
      if (id !== null) void steering.refresh();
    }),
  );

  // Reconciles the optimistic store with persisted rows. Reaches the drive at call time:
  // reconciling can discover a live run and hand it back to be driven.
  const resume = createResumeOps({
    conversationId: () => activeConversationId,
    fetchDetail: (id) => api.get<ConversationDetailDTO>(`/conversations/${id}`),
    messages,
    reseat,
    reseatMessages,
    reattachRun: (runId, opts) => drive.reattachRun(runId, opts),
  });

  const drive = createRunDrive({
    messages,
    setMessages,
    patchById,
    state: foldState,
    foldEvent,
    setSending,
    setErrored,
    titlePending,
    setTitlePending,
    collectTitle: collectLateTitle,
    deliveryDue: () => deliveryDue(steering.inbox()),
    followQueued: (due) => followQueued(due),
    conversationId: () => activeConversationId,
    reseatAfterRun: resume.reseatAfterRun,
    recoverLostRun: resume.recoverLostRun,
    refetchRun: resume.refetchRun,
    onConversationStarted: (id) => options.onConversationStarted?.(id),
    onTurnComplete: () => options.onTurnComplete?.(),
  });

  const approvals = createApprovalOps({
    messages,
    patchById,
    sending,
    activeRunId: () => foldState.activeRunId,
    reconcileStaleDecision: () => resume.reconcileStaleDecision(),
  });

  // Re-seed when the conversation changes, and never over a live one.
  createThreadBinding({
    key,
    initial,
    messages,
    boundTo: () => activeConversationId,
    onBind: (id) => {
      // Returning to a still-streaming thread must let the cold-reattach effect fire
      // again for the same run id.
      reattachedRunId = null;
      bindConversation(id);
      // A switch supersedes the drive that would clear these (`drive.ts` skips teardown
      // for a superseded run), so reset them here.
      setTitlePending(false);
      setRunClock(null);
      setRunQueue(null);
    },
    supersede: drive.supersede,
    setSending,
    state: foldState,
    setMessages,
    setUsage,
    setStats,
    setSnapshots,
    setTasks,
    setPlan,
    initialContext: options.initialContext,
    initialStats: options.initialStats,
    initialSnapshots: options.initialSnapshots,
  });

  /** Start a turn, or queue one into the live run. Resolves false as soon as the send is
   *  known not to have been taken (refused here or by the backend) so the composer can
   *  restore the text; true once an accepted turn's run has been driven to its end. */
  async function send(
    text: string,
    attachmentIds: string[] = [],
    extras: {
      /** Set by `continueTurn`: the stopped turn's branch node id, whose stop marker the
       *  backend retires. */
      continuesMessageId?: string;
      /** The slash command `text` started from; the backend resolves and expands it. */
      command?: { name: string; argument: string } | null;
      /** Workspace-relative paths named with `@`; references only, the model reads them. */
      fileRefs?: string[];
    } = {},
  ): Promise<boolean> {
    const { continuesMessageId, command, fileRefs } = extras;
    if (!text.trim() && attachmentIds.length === 0) return false;
    // The backend queues a send made during a turn; no optimistic row is drawn, the
    // inbox shows it once the backend has it.
    const live = sending();
    if (live && activeConversationId === null) {
      // The first turn's POST hasn't resolved, so there is no conversation to queue
      // against. Hand the text back to the composer, which already cleared itself.
      steering.stash(text);
      return true;
    }

    const wasNew = activeConversationId === null;
    if (!live) {
      setSending(true);
      if (wasNew && !options.ephemeral) setTitlePending(true);
    }
    const userMsg: ChatMessage = {
      id: nextId("u"),
      role: "user",
      content: text.trim(),
      createdAt: new Date().toISOString(),
      attachmentIds: attachmentIds.length ? attachmentIds : undefined,
    };
    // Only a per-instance override (compare panes) is sent; otherwise the backend
    // resolves the stored `main` binding. `selection` is just the display label.
    const override = options.selection?.();
    const selection = override ?? effectiveSelection();
    const assistantId = nextId("a");
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      model: selection?.model,
      content: "",
      blocks: [],
      streaming: true,
      queued: true,
      createdAt: new Date().toISOString(),
    };
    const showTurn = (): void =>
      setMessages(produce((m) => m.push(userMsg, assistantMsg)));
    const hideTurn = (): void => {
      setMessages(
        reconcile(
          messages.filter((m) => m.id !== userMsg.id && m.id !== assistantId),
        ),
      );
      setSending(false);
      setTitlePending(false);
    };
    if (!live) showTurn();

    let created: ChatSendDTO;
    const agent = options.agent?.(wasNew);
    try {
      created = await api.post<ChatSendDTO>("/chat", {
        prompt: text.trim(),
        conversation_id: activeConversationId ?? undefined,
        endpoint_id: override?.endpointId,
        model: override?.model,
        attachment_ids: attachmentIds,
        // Only meaningful on the send that creates the conversation.
        ephemeral: wasNew && options.ephemeral ? true : undefined,
        // Set once at creation; the backend re-reads it off the conversation after.
        project_id: wasNew ? options.projectId?.() : undefined,
        // Sent every time; the composer's control takes effect by being read here.
        permission_level: options.permission?.(),
        agent,
        continues_message_id: continuesMessageId,
        // Never an expansion: the backend decides what a command means.
        command: command ?? undefined,
        file_refs: fileRefs?.length ? fileRefs : undefined,
      });
    } catch (err) {
      // A queued send drew nothing, and the live run is not ours to end.
      if (!live) hideTurn();
      toast.error(
        (err as { detail?: string })?.detail ?? "Unable to reach Talos.",
      );
      return false;
    }
    bindConversation(created.conversation_id);
    if (agent !== undefined) options.onAgentSent?.(agent);
    if ("queued_message_id" in created) {
      // The conversation was busy, so the backend queued this send.
      if (!live) hideTurn();
      await steering.refresh();
      // Attach to a live run this client is not driving (another tab, or one started
      // between the room's last read and the send).
      if (created.run_id && created.run_id !== foldState.activeRunId)
        void resume.reattachToLiveRun(created.conversation_id);
      return true;
    }
    // The run `live` saw ended before the POST landed, so the backend started a fresh
    // turn for this send.
    if (live) {
      showTurn();
      setSending(true);
    }
    // The list read fired when `sending` flipped predates the new thread's id; re-read
    // so its row appears for the length of the first turn.
    if (wasNew && !options.ephemeral) refreshSessions();
    // The backend has retired the stop marker; echo that now rather than at run end.
    if (continuesMessageId !== undefined)
      patchById(continuesMessageId, (m) => {
        m.blocked = false;
        m.blockedDetail = undefined;
      });
    await drive.driveRun(created.run_id, assistantId, wasNew);
    return true;
  }

  /** Resume a stopped turn (timeout or cancel) by sending a fresh "Continue." turn.
   *  `messageId` is the stopped turn's branch node id, whose stop marker the backend
   *  retires on accept. */
  async function continueTurn(messageId?: string): Promise<void> {
    // No-op while a run is in flight, so "Continue." is not injected as steering.
    if (activeConversationId === null || sending()) return;
    await send(CONTINUE_PROMPT, [], { continuesMessageId: messageId });
  }

  /** Fold earlier turns, then resume the turn the context window stopped. Offered only
   *  on a context-overflow block, where a plain Continue would resend the same too-large
   *  request; the chassis tries this pair itself, and this is the manual fallback.
   *
   *  Unconditional on purpose: `compactNow` reports its own failure, and the send
   *  re-blocks if nothing was freed. The thread is checked after the fold because
   *  `continueTurn` re-reads the bound id and would otherwise send into a thread the
   *  operator moved to, with the old thread's `continuesMessageId`. */
  async function compactAndContinue(messageId?: string): Promise<void> {
    const conversationId = activeConversationId;
    if (conversationId === null) return;
    await branching.compactNow();
    if (activeConversationId !== conversationId) return;
    await continueTurn(messageId);
  }

  /** Queued messages go out on a turn the backend starts, which this client never
   *  submitted and so has no stream for. The settle read answers once that turn has
   *  started, naming its run: reseat and attach to it. With no run, reseat only when
   *  something was due (that turn may already have run to its end) and the operator's
   *  Stop is not what kept it from starting. */
  async function followQueued(due: boolean): Promise<void> {
    if (sending()) return;
    const read = await steering.settle();
    if (read === null || sending()) return;
    if (read.run_id === null && (!due || read.waiting_for_operator)) return;
    await resume.reconcileStaleDecision();
    // A turn that ended before it was found has no drive to report it.
    options.onTurnComplete?.();
  }

  /** Tell the backend to stop the run (it survives a dropped SSE), then abort the local
   *  stream and clear streaming state. Safe with no active run. */
  async function cancel(): Promise<void> {
    const runId = foldState.activeRunId;
    if (runId) {
      try {
        // Stop holds the inbox until the next send or an explicit resume.
        await api.post(`/runs/${runId}/cancel`, { hold_inbox: true });
      } catch (err) {
        // The local abort below still stops the UI.
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to cancel the run.",
        );
      }
    }
    foldState.activeRunId = null;
    // `stop`, not `supersede`: the drive must still run its teardown and reconcile.
    drive.stop();
    setMessages(
      produce((m) => {
        // `detached` (transport gave up) counts as in-flight too.
        const target = m.find((x) => x.streaming || x.detached);
        if (target) {
          target.streaming = false;
          target.queued = false;
          target.detached = false;
        }
      }),
    );
    setSending(false);
    // The drive's teardown also reads the inbox, but a detached drive skips it.
    void steering.refresh();
    // A kept draft would be seeded into the next park under a different question.
    approvals.clearParkDraft();
  }

  const branching = createBranchingOps({
    messages,
    setMessages,
    setUsage,
    setSnapshots,
    conversationId: () => activeConversationId,
    sending,
    setSending,
    patchById,
    overrideSelection: () => options.selection?.(),
    driveRun: (runId, assistantId) => drive.driveRun(runId, assistantId),
    reattachToLiveRun: (id) => resume.reattachToLiveRun(id),
    askRestore,
    onTurnComplete: () => options.onTurnComplete?.(),
  });

  // Cold-read reattach: a thread loaded mid-stream replays its in-flight run onto a
  // seeded assistant turn. `activeRun` is undefined while history loads, so it never
  // fires on an empty seed. It also picks up runs started out of sight (another tab, a
  // scheduled task). A run with a settled turn here was driven to its end by this
  // stream, and replaying its buffer would double every delta.
  createEffect(() => {
    const ar = options.activeRun?.();
    if (!ar || ar.id === reattachedRunId || ar.id === foldState.activeRunId)
      return;
    if (untrack(() => messages.some((m) => m.runId === ar.id && !m.streaming)))
      return;
    reattachedRunId = ar.id;
    if (!options.ephemeral && untrack(() => options.untitled?.()))
      setTitlePending(true);
    void drive.reattachRun(ar.id, attachOptions(ar));
  });

  return {
    messages,
    /** The thread being written into: the new id from the moment `POST /chat` answers,
     *  before `key()` catches up. Anything naming the thread on screen must read this. */
    conversationId: boundConversationId,
    /** The conversation's workspace snapshots (git-style history), newest last. */
    snapshots,
    toggleSnapshotKeeper: branching.toggleSnapshotKeeper,
    sending,
    errored,
    /** True while the live run's transport is detached (reconnect budget
     *  exhausted) — the run may still be alive server-side, awaiting a
     *  manual/automatic re-attach rather than being over. */
    detached: drive.detached,
    /** True while the run is parked on a decision or question. The main room mirrors it
     *  to the global `awaitingInput` echo (nav rail warn tone, favicon tint). */
    awaitingInput: approvals.awaitingInput,
    /** What the live turn is parked on, or null; rendered by the dock. */
    park: approvals.park,
    titlePending,
    reattaching: drive.reattaching,
    usage,
    stats,
    /** The agent's running checklist for this thread. */
    tasks,
    /** The written plan the operator approves, distinct from the task list. */
    plan,
    /** Run start and current step, or null between runs. */
    runClock,
    /** Where the run waits for its model endpoint, or null when it is not waiting. */
    runQueue,
    /** The run currently streaming into this store, or null. */
    activeRunId: () => foldState.activeRunId,
    /** Highest event seq folded so far — the resume point for a reattach. */
    lastSeq: () => foldState.maxFoldedSeq,
    send,
    /** Resume a turn a bound stopped by sending a fresh "Continue." turn. */
    continueTurn,
    /** Fold the thread, then resume the turn the context window stopped. */
    compactAndContinue,
    cancel,
    /** What is queued behind the running turn, and whether Stop is holding it. */
    inbox: steering.inbox,
    /** Withdraw an operator message that has not yet reached the model. */
    withdrawQueued: (id: string) => steering.withdraw(id),
    /** Rewrite an operator message that has not yet reached the model. */
    editQueued: (id: string, text: string) => steering.edit(id, text),
    holdQueued: (id: string, held: boolean) => steering.hold(id, held),
    /** Release a Stop's hold so the waiting messages are delivered. */
    resumeInbox: steering.resume,
    /** Text handed back to the composer; the screen prefills it, then clears it. */
    undeliveredDraft: steering.undeliveredDraft,
    clearUndeliveredDraft: steering.clearUndeliveredDraft,
    /** Hand back text from a refused send made outside the composer. */
    stashDraft: steering.stash,
    reattachRun: drive.reattachRun,
    resolvePark: approvals.resolvePark,
    /** Unsubmitted dock input, held here because a re-derived park unmounts the dock. */
    parkDraft: approvals.parkDraft,
    patchParkDraft: approvals.patchParkDraft,
    resolveHostCommands: approvals.resolveHostCommands,
    regenerate: branching.regenerate,
    edit: branching.edit,
    switchVersion: branching.switchVersion,
    rewind: branching.rewind,
    compactNow: branching.compactNow,
    /** True while a hand-started fold runs on the shown thread. Composed here because it
     *  needs the reactive bound id; the branching ops read the plain `let`. */
    compacting: (): boolean => {
      const id = boundConversationId();
      return id !== null && branching.isCompacting(id);
    },
    removeMessage: branching.removeMessage,
    toggleMessagePin: branching.toggleMessagePin,
  };
}
