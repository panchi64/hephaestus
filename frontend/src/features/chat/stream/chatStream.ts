/**
 * The thread controller — one conversation's live state, and the ops the room drives it
 * with.
 *
 * What is left here after the split is the *assembly*: the signals a thread's UI reads,
 * the ops that start and stop a turn, and the wiring that lets six single-purpose modules
 * behave as one object. Each of those modules answers a question this file deliberately no
 * longer does — driving a run's transport (`drive.ts`), what a frame changes on screen
 * (`fold.ts`), a message sent into a run already going (`steering.ts`), deciding the calls
 * a run parked on (`approvals.ts`), reconciling with what was persisted (`resume.ts`),
 * acting on a turn already recorded (`branching.ts`), and being pointed at a different
 * thread (`binding.ts`).
 *
 * **The store is authoritative for the thread it is on, and the backend is authoritative
 * for what a turn was.** The operator's prompt is persisted the moment a turn begins, but
 * its answer only when it finishes, so a refetch mid-stream reads the prompt and none of
 * the streaming reply. That is why `binding.ts` refuses to re-seed a live thread — and why,
 * once the run ends, `resume.ts` reseats the transcript from the backend's rows: the
 * optimistic echo and the streamed bubble give way to the ids the backend minted, with no
 * positional guesswork about which is which.
 *
 * **The ops form a cycle, and it is a real one.** A run's teardown reconciles the thread,
 * reconciling can discover a live run and start a drive, and a drive hands undelivered
 * steering text back. Two of the wirings below therefore reach the drive at *call* time
 * rather than holding its handle, which is what makes the cycle two named edges instead of
 * one closure in which every part can reach every other.
 */

import { createEffect, createSignal, untrack } from "solid-js";
import { createStore, produce, reconcile } from "solid-js/store";
import { api, isApiError } from "~/lib/api";
import { effectiveSelection, type ModelSelection } from "~/lib/stores/models";
import type { TaskItem } from "~/lib/stream";
import type { PlanDocument } from "../model";
import { toast } from "~/ui";
import { CONTINUE_PROMPT } from "../data/constants";
import { refreshSessions } from "../data/sessions";
import type { ChatCreatedDTO, ConversationDetailDTO } from "../data/wire";
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
import {
  createBranchingOps,
  reseatFromDetail,
  reseatMessagesFromDetail,
  type TranscriptStore,
} from "./branching";
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

export interface ChatStreamOptions {
  /** Fired once when a brand-new conversation receives its backend id. */
  onConversationStarted?: (id: string) => void;
  /** Fired when a turn finishes (done or errored) — refresh the session list. */
  onTurnComplete?: () => void;
  /** Override the model this stream's turns run on, instead of the global picker
   *  selection. Used by the compare panes, which each own a per-pane model. */
  selection?: () => ModelSelection | null;
  /** Mark a freshly-created conversation as scratch (hidden from the sidebar
   *  listing). Used by compare panes — throwaway threads, not saved history. */
  ephemeral?: boolean;
  /** The project the *next new* conversation is scoped to, if any. Read only when a
   *  send creates the conversation — the binding is immutable afterwards, and the
   *  backend owns it from then on. */
  projectId?: () => string | undefined;
  /** How far the model may go this turn — and from this turn onwards. Unlike the
   *  project, sent on **every** send: the level is the operator's live control, so
   *  switching it mid-thread is a plain message rather than a separate call, and the
   *  backend persists whatever the last send named. */
  permission?: () => PermissionLevel;
  /** The custom agent to name in this send, if the operator changed it. Asked with
   *  whether the send creates the thread, since the default is only worth naming on
   *  an existing one. Omitted fields leave the thread's agent as it is. */
  agent?: (isNewThread: boolean) => string | undefined;
  /** Fired once a send that named an agent was accepted. */
  onAgentSent?: (name: string) => void;
  /** Fired when the **run** moved the level — `plan_enter` narrowing it, or an approved
   *  plan raising it. Necessary rather than informational: the level above is sent back
   *  on every message, so a caller that did not hear this would write the stale one over
   *  the change on the operator's next send. */
  onPermissionChanged?: (level: PermissionLevel) => void;
  /** The loaded conversation's context-window state, seeded alongside its history
   *  so an existing thread shows window fullness before its next turn runs. */
  initialContext?: () => ContextUsage | null | undefined;
  /** The loaded conversation's cumulative readout, seeded the same way. The backend
   *  rebuilds it from the stored messages, so the line under the composer says the
   *  same thing after a reload as it did while the thread was live. */
  initialStats?: () => ConversationStats | null | undefined;
  /** The loaded conversation's in-flight run, if a turn is still streaming
   *  server-side. Seeds a reattach on a cold read (e.g. a page reload mid-stream)
   *  so the live answer continues instead of the thread rendering reply-less. */
  activeRun?: () => ActiveRun | null | undefined;
  /** The loaded conversation's workspace snapshots (git-style history), seeded
   *  alongside its messages so the viewport shows them before the next turn. */
  initialSnapshots?: () => ViewSnapshotRef[] | undefined;
}

export function createChatStream(
  initial: () => ChatMessage[] | undefined,
  key: () => string | null = () => null,
  options: ChatStreamOptions = {},
) {
  const [messages, setMessages] = createStore<ChatMessage[]>([]);
  // Conversation-level workspace snapshots (git-style history). Unlike versions/
  // live (which fold onto message blocks), snapshots are conversation-scoped, so
  // they live here beside the messages rather than in the transcript.
  const [snapshots, setSnapshots] = createSignal<ViewSnapshotRef[]>([]);
  const [sending, setSending] = createSignal(false);
  // True when this room's last run ended in `run.error`; cleared when the next run
  // starts (in the drive). The main room mirrors it to the global `runErrored` echo
  // so the favicon can flag a failed run — compare panes keep it local.
  const [errored, setErrored] = createSignal(false);
  // A brand-new thread is auto-named during its first turn; this drives a "working"
  // throbber on the title from that turn's start until the name lands
  // (`conversation.titled`, which the reveal then animates) or the turn ends without
  // one. Backend-owned outcome — the frontend only reflects the in-flight window.
  const [titlePending, setTitlePending] = createSignal(false);
  // The latest run's context-window state, as derived by the backend. Null until
  // a run reports it against a known window (loaded history carries none), which
  // is when the context meter first appears.
  const [usage, setUsage] = createSignal<ContextUsage | null>(null);
  // What the thread has cost — the composer's stats panel. One signal, because the
  // backend sends one shape: the live `run.metrics` frame and the conversation load's
  // `stats` are the same payload, so a reload continues the same numbers rather than
  // blanking them. (This was three signals off one event; the counts and the token
  // counts were never separate facts.)
  const [stats, setStats] = createSignal<ConversationStats | null>(null);
  // The agent's task list for this thread. Conversation-level rather than a message
  // block: one list belongs to the thread and is rewritten in place as work proceeds,
  // so pinning it to the turn that happened to create it would strand it.
  // `tasks.updated` carries the whole list, so applying an event is a replace, never a
  // merge.
  const [tasks, setTasks] = createSignal<TaskItem[]>([]);
  // The written plan this thread is working to, when it has one. One per thread and
  // replaced on every revision, so — like the list above — it is a signal rather than
  // anything pinned to a message.
  const [plan, setPlan] = createSignal<PlanDocument | null>(null);
  // The streaming run's start instant and step, for the composer's header — read off
  // the run's own events, so the clock it drives measures the backend's run.
  const [runClock, setRunClock] = createSignal<RunClock | null>(null);
  const [runQueue, setRunQueue] = createSignal<RunQueue | null>(null);
  // The run-scoped bookkeeping the fold advances and the drive resets: the high-water
  // seq, the bubble events land on, the two backfill-race counters, and the run
  // currently streaming. One object, shared by reference with the folder.
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
  // The last run a cold-read reattach was kicked off for, so the load effect fires
  // at most once per run even if the session resource re-emits the same value.
  let reattachedRunId: string | null = null;
  // The conversation this stream is currently bound to (tracked separately from
  // the screen's `key`, which only updates once a new thread is persisted).
  //
  // **Held twice on purpose: a plain `let` and a signal, written together.** Almost
  // everything here reads it synchronously, mid-callback, where a signal read would
  // register a dependency in whatever effect happens to be running — so the `let`
  // stays. But the room has to be able to *render* against it: a thread created
  // during its first turn has a real id from the moment `POST /chat` answers, and
  // anything the screen keys on `key()` alone (the header's name, the rail's
  // selection) sits blank until the turn ends. `boundConversationId` is that same
  // fact, reactive.
  let activeConversationId: string | null = key();
  const [boundConversationId, setBoundConversationId] = createSignal<
    string | null
  >(activeConversationId);
  /** Write both halves. Every assignment to `activeConversationId` goes through
   *  here — a bare write would leave the reactive copy behind, which is exactly the
   *  class of bug this pairing exists to fix. */
  const bindConversation = (id: string | null): void => {
    activeConversationId = id;
    setBoundConversationId(id);
  };

  const store: TranscriptStore = { setMessages, setUsage, setSnapshots };
  const reseat = (detail: ConversationDetailDTO) =>
    reseatFromDetail(store, detail);
  const reseatMessages = (detail: ConversationDetailDTO) =>
    reseatMessagesFromDetail(store, detail);

  const patchById = createPatchById(messages, setMessages);

  const foldEvent = createFolder({
    state: foldState,
    patchById,
    setMessages,
    setSnapshots,
    setTasks,
    setPlan,
    // The one fact the run can change that the client also holds and sends back, so
    // the caller has to hear about it — see `FoldDeps.setPermission`. Absent for a
    // caller with no level of its own (the compare pane), where a no-op is right.
    setPermission: (level) => options.onPermissionChanged?.(level),
    setUsage,
    setStats,
    setErrored,
    setTitlePending,
    setRunClock,
    setRunQueue,
    // A checkpoint that names a turn this transcript does not hold: the persisted
    // thread is ahead of it, so take its word rather than a guessed position. Reached
    // at call time — the resume ops are built below, and need the drive that needs this.
    onFoldMisplaced: () => void resume.reconcileStaleDecision(),
  });

  // Steering: a message sent into a run that is already going, and the text handed
  // back when one never reached the model. It reaches the drive declared below
  // through a call-time closure rather than a handle — the cycle is real (a queue
  // attempt can turn into a fresh turn to drive, and a drive's teardown hands back
  // what it never delivered), and naming the edge is better than merging the two.
  const steering = createSteeringOps({
    messages,
    setMessages,
    patchById,
    conversationId: () => activeConversationId,
    adoptConversationId: bindConversation,
    activeRunId: () => foldState.activeRunId,
    setSending,
    selection: () => options.selection?.() ?? effectiveSelection(),
    driveRun: (runId, assistantId) => drive.driveRun(runId, assistantId),
  });

  // Everything that reconciles the optimistic store with what the backend persisted.
  // It lives next door because all four are one move — read the detail, act on it only
  // if the operator is still on that thread — and that guard belongs in one place.
  // Same call-time edge to the drive, for the same reason: reconciling can discover a
  // run that is still live and hand it straight back to be driven.
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
    setTitlePending,
    restoreUndelivered: steering.restoreUndelivered,
    conversationId: () => activeConversationId,
    reseatAfterRun: resume.reseatAfterRun,
    recoverLostRun: resume.recoverLostRun,
    refetchRun: resume.refetchRun,
    onConversationStarted: (id) => options.onConversationStarted?.(id),
    onTurnComplete: () => options.onTurnComplete?.(),
  });

  // Deciding the calls a run parked on, and whether anything is still parked.
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
      // Forget which run we've already reattached-to: leaving this thread (still
      // detached/mid-stream) and returning later must let the cold-reattach effect
      // fire again for the same run id, rather than permanently ignoring a run it
      // saw once, in some earlier visit, before this stream instance existed.
      reattachedRunId = null;
      bindConversation(id);
      // The auto-title throbber belongs to the thread being named, and a switch
      // supersedes the drive that would otherwise clear it (`drive.ts` skips teardown
      // for a superseded run, deliberately) — so without this the first turn's
      // throbber follows the operator onto every thread they open next.
      setTitlePending(false);
      // The clock is the run's, and the run is the thread's: a switch supersedes the
      // drive whose `run.ended` would have cleared it.
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

  /** Start a turn, or queue one into the live run. Resolves **false** as soon as the
   *  send is known not to have been taken — refused here or by the backend — so the
   *  composer can put the text back; **true** once an accepted turn's run has been
   *  driven to its end (callers such as Compare await the whole run through this). A
   *  caller that only wants the refusal reads the `false` and ignores the timing. */
  async function send(
    text: string,
    attachmentIds: string[] = [],
    /** Everything else this send carries. An options object rather than more
     *  positional parameters: the third slot was `continuesMessageId`, called
     *  positionally by `continueTurn`, and appending to it would have made every
     *  later addition a place where two unrelated facts could be swapped. */
    extras: {
      /** Set only by `continueTurn`: the branch node id of the stopped turn this
       *  send resumes, so the backend retires that turn's stop marker for good. */
      continuesMessageId?: string;
      /** The slash command this turn was started from. `text` is still the
       *  operator's literal `/name …`; this says which command that token was, so
       *  the backend can resolve and expand it. */
      command?: { name: string; argument: string } | null;
      /** Workspace-relative paths the operator named with `@`. A reference only —
       *  the backend names them to the model, which reads what it wants. */
      fileRefs?: string[];
    } = {},
  ): Promise<boolean> {
    const { continuesMessageId, command, fileRefs } = extras;
    // A turn needs either prompt text or at least one attachment to send.
    if (!text.trim() && attachmentIds.length === 0) return false;
    if (sending()) {
      // Mid-run steering is text-only — an attachment can't ride an existing
      // run's request (and the composer disables attach while streaming).
      if (attachmentIds.length > 0) {
        toast.error(
          "Attachments can't be added while a response is in progress.",
        );
        return false;
      }
      // Nor can a command. A queued message is injected verbatim, with no prelude
      // around it to resolve one through — so `/deploy prod` would reach the model
      // as the literal token. The backend refuses it for the same reason; saying so
      // here keeps the operator's typed message rather than spending it on a 409.
      // Nor a file reference, for the same reason and with the same backend refusal.
      if (command || fileRefs?.length) {
        toast.error(
          "A command or file reference can't be sent while a response is in progress.",
        );
        return false;
      }
      if (!text.trim()) return false;
      return steering.sendWhileStreaming(text);
    }
    setSending(true);

    const wasNew = activeConversationId === null;
    // A fresh, non-ephemeral thread gets auto-named during this turn; show the
    // working throbber on the title until it lands. Ephemeral threads aren't titled.
    if (wasNew && !options.ephemeral) setTitlePending(true);
    const userMsg: ChatMessage = {
      id: nextId("u"),
      role: "user",
      content: text.trim(),
      createdAt: new Date().toISOString(),
      attachmentIds: attachmentIds.length ? attachmentIds : undefined,
    };
    // `override` is a genuine per-instance model (the compare panes); the default
    // path sends none, so the backend resolves the stored `main` binding — the same
    // source research/tasks/titling resolve. `selection` is only the display label.
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
    setMessages(produce((m) => m.push(userMsg, assistantMsg)));

    let created: ChatCreatedDTO;
    const agent = options.agent?.(wasNew);
    try {
      created = await api.post<ChatCreatedDTO>("/chat", {
        prompt: text.trim(),
        conversation_id: activeConversationId ?? undefined,
        endpoint_id: override?.endpointId,
        model: override?.model,
        attachment_ids: attachmentIds,
        // Only meaningful when this turn creates the conversation; the backend
        // ignores it when continuing one.
        ephemeral: wasNew && options.ephemeral ? true : undefined,
        // Likewise: a thread's project is set once, at creation. The backend
        // re-reads it off the conversation for every later turn, so sending it again
        // would be a second source for one fact.
        project_id: wasNew ? options.projectId?.() : undefined,
        // The level, on the other hand, is sent every time: it is the one binding
        // fact that moves, and the composer's control moves it by being read here on
        // the next send rather than by a write of its own.
        permission_level: options.permission?.(),
        agent,
        continues_message_id: continuesMessageId,
        // Never an expansion — what a command *means* is the backend's to decide, and a
        // client that sent one would be sending a turn the operator never wrote.
        command: command ?? undefined,
        file_refs: fileRefs?.length ? fileRefs : undefined,
      });
    } catch (err) {
      // Not accepted, for either reason — so the optimistic turn comes back out of the
      // transcript, and the `false` hands its text back to the composer. **One rule
      // for both failures**: a send the backend did not take is not a turn, and it
      // goes back where it was written. The generic arm used to leave the bubble
      // standing under a dead assistant reply, which read as a turn that happened and
      // failed — and the words were gone from the field, so retrying meant retyping.
      setMessages(
        reconcile(
          messages.filter((m) => m.id !== userMsg.id && m.id !== assistantId),
        ),
      );
      setSending(false);
      setTitlePending(false);
      if (isApiError(err) && err.status === 409) {
        // A run is already active on this conversation — surface the one that's
        // actually in flight instead of silently discarding this send.
        toast.error("A response is still in progress in this conversation.");
        if (activeConversationId)
          void resume.reattachToLiveRun(activeConversationId);
        return false;
      }
      toast.error(
        (err as { detail?: string })?.detail ?? "Unable to reach Talos.",
      );
      return false;
    }
    bindConversation(created.conversation_id);
    if (agent !== undefined) options.onAgentSent?.(agent);
    // A brand-new thread exists the moment the backend accepts the send. The list read
    // fired when `sending` flipped went out before this id did, so it cannot have
    // listed the thread; read it again now so the row (and its running echo) appears
    // for the length of the first turn instead of at its end.
    if (wasNew && !options.ephemeral) refreshSessions();
    // Accepted — so the backend has retired the stop marker this turn resumes.
    // Echo that now rather than waiting for the run to finish: the warning is about
    // a turn the operator has visibly just resumed, and leaving it up for the length
    // of the new run reads as the Continue press having done nothing.
    if (continuesMessageId !== undefined)
      patchById(continuesMessageId, (m) => {
        m.blocked = false;
        m.blockedDetail = undefined;
      });
    await drive.driveRun(created.run_id, assistantId, wasNew);
    return true;
  }

  /** Resume a turn a bound stopped (inactivity/wall-clock timeout or cancel) by
   *  sending a fresh "Continue." turn on the same conversation. Reuses the ordinary
   *  send path, so a small "Continue." bubble appears in the transcript and the
   *  model picks up where the prior turn left off.
   *
   *  ``messageId`` is the stopped turn's branch node id — the backend retires that
   *  turn's stop marker when it accepts the turn, so the warning doesn't linger
   *  under a turn the operator already resumed, and a reload reads the retirement
   *  back from the store rather than resurrecting it. */
  async function continueTurn(messageId?: string): Promise<void> {
    // A blocked turn is settled, so this is a fresh turn — but if a run is already
    // in flight (the operator started a new one), don't inject "Continue." as a
    // steering message; just no-op.
    if (activeConversationId === null || sending()) return;
    await send(CONTINUE_PROMPT, [], { continuesMessageId: messageId });
  }

  /** Fold this thread's earlier turns, then resume the turn the model's context window
   *  stopped — the two ops the operator would otherwise run by hand, in the order that
   *  makes the second one able to succeed.
   *
   *  Offered only on a turn blocked with the context-overflow detail, because that is
   *  the one stop where a plain Continue is guaranteed to fail: the request that was
   *  too large is the request that would be sent again. The chassis attempts this pair
   *  itself inside the turn; this is the hand-operated version for when it could not —
   *  compaction switched off for the thread, no background model bound, or a fold that
   *  freed too little.
   *
   *  Sequential and unconditional on purpose: `compactNow` reports its own failure and
   *  the send below re-blocks on the same bound if nothing was freed, so the operator
   *  ends up where they started rather than somewhere new. Nothing here decides whether
   *  the fold was enough — that judgement is the backend's, on the next request.
   *
   *  The thread is captured before the fold and checked after it, because the fold holds
   *  a summarizer call — seconds on a local model — and the operator is free to leave
   *  mid-flight. `continueTurn` re-reads the bound id, so without this the pair would
   *  send a "Continue." turn into whichever thread the room had moved to, carrying the
   *  *old* thread's `continuesMessageId` and retiring a stop marker on a branch node that
   *  does not exist there. The turn the operator asked to resume is still resumable from
   *  its own thread when they come back to it. */
  async function compactAndContinue(messageId?: string): Promise<void> {
    const conversationId = activeConversationId;
    if (conversationId === null) return;
    await branching.compactNow();
    if (activeConversationId !== conversationId) return;
    await continueTurn(messageId);
  }

  /** Cancel the in-flight run for real: tell the backend to stop it (it keeps
   *  running even when the SSE is dropped), then abort the local stream and clear
   *  the streaming state. Safe to call with no active run. */
  async function cancel(): Promise<void> {
    const runId = foldState.activeRunId;
    if (runId) {
      try {
        await api.post(`/runs/${runId}/cancel`, {});
      } catch (err) {
        // The local abort below still stops the UI; surface but don't block.
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to cancel the run.",
        );
      }
    }
    foldState.activeRunId = null;
    // `stop`, not `supersede`: the turn really is over, so the drive still has to
    // run its own teardown and reconcile what was persisted.
    drive.stop();
    setMessages(
      produce((m) => {
        // A cancelled turn may currently be `streaming` (normal in-flight) or
        // `detached` (transport gave up, but we still guarded it as in-flight)
        // — either is the one this cancel is aimed at.
        const target = m.find((x) => x.streaming || x.detached);
        if (target) {
          target.streaming = false;
          target.queued = false;
          target.detached = false;
        }
      }),
    );
    setSending(false);
    // A cancel with steering messages still queued: they'll never be injected
    // now, so restore them to the composer. (The drive's own teardown also calls
    // this; it's idempotent, and this covers the detached case it skips.)
    steering.restoreUndelivered();
    // Whatever was half-answered in the dock goes with the run it was answering. Kept
    // any longer it would be seeded into the *next* park — a different question wearing
    // the last one's answers.
    approvals.clearParkDraft();
  }

  // Branching, version-cycling and the rest of what can be done to a recorded turn.
  // They act on the conversation tree rather than on the run in flight, so they take
  // this controller's handles rather than living inside it.
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
    reattachToLiveRun: resume.reattachToLiveRun,
    cancel,
    onTurnComplete: () => options.onTurnComplete?.(),
  });

  // Cold-read reattach: a thread loaded mid-stream carries its in-flight run
  // (`options.activeRun`), so resume it — fold the full replay onto a freshly
  // seeded assistant turn and continue live — instead of rendering the thread
  // reply-less. The source is withheld (→ undefined) while history loads, so this
  // never fires on an empty seed; it runs once per run (`reattachedRunId`) and not
  // for a run we're already driving.
  //
  // It is also how a run started out of sight is picked up (another tab, a scheduled
  // task): the room re-reads the thread when the rail reports it busy, and the new
  // `activeRun` lands here. A run this transcript already holds a settled turn for is
  // one this stream drove to its end — the backend can still list it for a moment
  // after — and replaying its buffer over that turn would double every delta.
  createEffect(() => {
    const ar = options.activeRun?.();
    if (!ar || ar.id === reattachedRunId || ar.id === foldState.activeRunId)
      return;
    if (untrack(() => messages.some((m) => m.runId === ar.id && !m.streaming)))
      return;
    reattachedRunId = ar.id;
    void drive.reattachRun(ar.id, attachOptions(ar));
  });

  return {
    messages,
    /** The thread this stream is writing into — the screen's `key` once it is caught
     *  up, and the backend's brand-new id from the moment `POST /chat` answers, which
     *  is well before the room adopts it. Anything that *names the thread on screen*
     *  has to read this: keyed on `key()` alone, a first turn's header and rail row
     *  have no id to resolve against and stay blank for the length of the run, which
     *  is why an auto-generated title used to land only once the stream ended. */
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
    /** True while this run is parked on the operator — a sensitive tool call awaiting
     *  their decision, or a question awaiting their answer. The main room mirrors it to
     *  the global `awaitingInput` echo (nav rail warn tone, favicon attention tint). */
    awaitingInput: approvals.awaitingInput,
    /** What the live turn is parked on, or null. The dock over the composer renders it. */
    park: approvals.park,
    titlePending,
    reattaching: drive.reattaching,
    usage,
    stats,
    /** The agent's running checklist for this thread. */
    tasks,
    /** The written plan it is working to, when there is one — the document the operator
     *  approves, which is a different thing from the list above. */
    plan,
    /** When the streaming run started and which step it is on, or null between runs. */
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
    /** Withdraw a queued (not-yet-injected) steering message from the live run. */
    withdrawQueued: (id: string) => steering.withdrawQueued(id),
    /** Rewrite a queued (not-yet-injected) steering message in place. */
    editQueued: (id: string, text: string) => steering.editQueued(id, text),
    holdQueued: (id: string, held: boolean) => steering.holdQueued(id, held),
    /** Text of queued messages the run never consumed (restored on terminal) —
     *  the screen prefills the composer with it, then clears it. */
    undeliveredDraft: steering.undeliveredDraft,
    clearUndeliveredDraft: steering.clearUndeliveredDraft,
    /** Hand text back to the composer through the same prefill — for a send made on
     *  the operator's behalf, outside the composer, that the backend refused. */
    stashDraft: steering.stash,
    reattachRun: drive.reattachRun,
    resolvePark: approvals.resolvePark,
    /** What the operator has entered into the dock but not yet submitted — held here
     *  rather than in the dock, which a re-derived park unmounts. */
    parkDraft: approvals.parkDraft,
    patchParkDraft: approvals.patchParkDraft,
    resolveHostCommands: approvals.resolveHostCommands,
    regenerate: branching.regenerate,
    edit: branching.edit,
    switchVersion: branching.switchVersion,
    rewind: branching.rewind,
    compactNow: branching.compactNow,
    /** True while a hand-started fold is in flight on the thread the room is showing.
     *
     *  Composed here rather than inside the branching ops because the answer needs the
     *  *reactive* half of the bound id: the ops read `activeConversationId` imperatively,
     *  and a `compacting` built on that would never re-run on a thread switch — it would
     *  keep reporting the thread the operator left. */
    compacting: (): boolean => {
      const id = boundConversationId();
      return id !== null && branching.isCompacting(id);
    },
    removeMessage: branching.removeMessage,
    toggleMessagePin: branching.toggleMessagePin,
  };
}
