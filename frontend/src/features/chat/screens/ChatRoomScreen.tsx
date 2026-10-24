import {
  Show,
  createEffect,
  createMemo,
  createResource,
  createSignal,
  on,
  onCleanup,
  onMount,
  untrack,
  type JSX,
} from "solid-js";
import {
  Composer,
  ScrollFade,
  StatusBar,
  StatusCell,
  cx,
  toast,
  type ComposerMenuItem,
} from "~/ui";
import {
  conversationGrantsRevision,
  chatDraftKey,
  consumePendingDraft,
  consumeRequestedSession,
  entrySessionId,
  fetchOwnCallsign,
  mainChat,
  refreshSessions,
  titleReveals,
  useChatSessions,
} from "../data";
import { sendBlockedReason, setSelectedModel } from "~/lib/stores/models";
import {
  setStagedProjectId,
  stagedProjectId,
} from "~/lib/stores/stagedProject";
import { createComposerAttachments } from "~/features/uploads/data";
import { useProjects } from "~/lib/stores/projects";
import { settled } from "~/lib/resource";
import { directoryLabel } from "../addDirectory";
import { ChatRoomHeader } from "../components/ChatRoomHeader";
import { ChatViewportMounts } from "../components/ChatViewportMounts";
import { AllowHostContext } from "../allowHostContext";
import { allowDomain } from "../data/egress";
import { stopThreadAgents } from "../data/subagents";
import { stopRoute } from "../stopRoute";
import {
  ComposerHeaderAside,
  ComposerHeaderStatus,
} from "../components/ComposerHeaderStatus";
import { createConversationStatus } from "../components/ConversationStatus";
import { ParkDock } from "../components/ParkDock";
import { PermissionControl } from "../components/PermissionControl";
import { AgentControl } from "../components/AgentControl";
import { ProjectControl } from "../components/ProjectControl";
import { useAgents } from "~/features/agents/data";
import { agentDisplayName, threadAgentName } from "~/features/agents/model";
import { ThreadRecap } from "../components/ThreadRecap";
import { TranscriptView } from "../components/TranscriptView";
import { ModelPicker } from "~/app/ModelPicker";
import { createBrowserState } from "../browserState";
import { createConversationActions } from "../conversationActions";
import { registerChatRoomKeymap } from "../chatRoomKeymap";
import { useChatViewport } from "../useChatViewport";
import { createThreadChanges } from "../branchState";
import { createSubagentsState } from "../subagentsState";
import { createTranscriptFollow } from "../transcriptScroll";
import { isNamedInPlace } from "../stream/binding";
import { createRenameConversation } from "../components/RenameConversationModal";
import { createComposerCommands } from "../commands/useComposerCommands";
import { createComposerFileRefs } from "../files/useFileRefs";
import { InboxDock } from "../components/InboxDock";
import { createComposerRecall } from "../composerRecall";
import { nextPermissionLevel, parsePermissionLevel } from "../model";

/** One reading measure shared by the transcript's content and the composer dock so the
 *  input matches the column above it. Only contents are centred; the scroll container
 *  and the dock background stay full width so the transcript scrolls behind the dock. */
const MEASURE = "mx-auto w-full max-w-4xl";

/** Chat room: a live streaming conversation. On entry it resumes the last conversation
 *  only while it's warm (recency-gated), otherwise it opens a fresh composer; the
 *  overview launchpad can also hand it a thread to open or a message to start. */
export function ChatRoomScreen(): JSX.Element {
  const sessions = useChatSessions();
  // Stream, selected conversation (null = new, unsaved) and history live in a
  // module-level controller so navigating away mid-turn doesn't tear down the run.
  const {
    currentId,
    openId,
    setCurrentId,
    history,
    reloadHistory,
    stream,
    browserOpenedSeq,
    warmResolved,
    markWarmResolved,
    permission,
    setPermission,
    permissionPending,
    agentChoice,
    setAgentChoice,
  } = mainChat();
  const projects = useProjects();

  /** The project a staged thread would be scoped to; undefined once saved. Shared by the
   *  header subtitle and the composer picker.
   *
   *  Staged means no backend id anywhere (`openId`, not `currentId`): the seat stays
   *  empty through a new thread's first turn, so `currentId` would still read as staged. */
  const stagedProject = createMemo(() => {
    if (openId() !== null) return undefined;
    const id = stagedProjectId();
    return id
      ? settled(projects)?.projects.find((p) => p.id === id)
      : undefined;
  });

  // See `transcriptScroll.ts` for the seating behaviour.
  const transcript = createTranscriptFollow({
    sending: stream.sending,
    conversationId: currentId,
    boundId: stream.conversationId,
    anchorTurnId: () =>
      stream.messages.findLast((m) => m.role === "user")?.id ?? null,
  });

  // `openId`, not `currentId`: during a new thread's first turn the seat is not yet on the
  // backend's id, so the auto-generated name could not resolve until the stream ended.
  const currentSummary = createMemo(() => {
    const id = openId();
    return id ? sessions()?.find((s) => s.id === id) : undefined;
  });
  const headerTitle = () => currentSummary()?.title ?? "New conversation";

  /** Which thread the recap was dismissed for; view state only, keyed by thread so one
   *  dismissal doesn't hide the next thread's recap. */
  const [recapDismissed, setRecapDismissed] = createSignal<string | null>(null);
  const recapSummary = createMemo(() =>
    recapDismissed() === openId() ? undefined : currentSummary(),
  );
  const headerReveal = () => {
    const id = openId();
    return id ? titleReveals[id] : undefined;
  };

  // Resolve the entry intent: new-from-overview › open-specific › recency.
  createEffect(() => {
    // Launchpad intents apply on every entry, even after the one-time warm resume below.
    const draft = consumePendingDraft();
    if (draft) {
      // An empty draft (discovery unresolved on the overview) must not clobber the sticky selection.
      if (draft.model) void setSelectedModel(draft.model);
      // Applied before the thread is created so the first turn runs at this level.
      if (draft.permissionLevel) setPermission(draft.permissionLevel);
      setCurrentId(null);
      queueMicrotask(
        () =>
          void stream.send(draft.text, draft.attachmentIds, {
            // Without it the literal `/reviewer …` would be sent with nothing marking the token as a command.
            command: draft.command ?? null,
          }),
      );
      markWarmResolved();
      return;
    }
    const requested = consumeRequestedSession();
    if (requested) {
      setCurrentId(requested);
      markWarmResolved();
      return;
    }
    // Once per session: room state persists across navigation, so a remount must not
    // move the operator off the thread (or fresh composer) they left open.
    if (warmResolved()) return;
    const list = sessions();
    if (!list) return;
    if (untrack(currentId) === null) setCurrentId(entrySessionId(list));
    markWarmResolved();
  });

  /** The staged project as the header names it; none for a saved thread (its branch chip covers that). */
  const workspaceHint = (): string | undefined => {
    const project = stagedProject();
    return project ? directoryLabel(project) : undefined;
  };

  /** Unconditional: used when the shown thread is gone, so it can never decline. */
  const clearThread = () => setCurrentId(null);

  /** A fresh composer on request; it keeps the current thread's project. From an already
   *  staged composer it changes nothing. */
  const startNew = () => {
    if (openId() !== null) setStagedProjectId(currentSummary()?.projectId);
    clearThread();
  };

  const allowHost = async (host: string): Promise<void> => {
    const id = currentId();
    if (id !== null) await allowDomain(id, host);
  };
  const route = () =>
    stopRoute({
      sending: stream.sending(),
      subagents: subagents.latest(),
    });
  // Cancels on the backend and aborts the local stream; `cancel()` reports its own error.
  // A correction typed into the composer rides the same act, so it isn't retyped or
  // overtaken by a restoring queued message. The stopped turn is kept intact and the
  // correction opens the next turn against it.
  const stopRun = async (correction?: string) => {
    const which = route();
    if (which === "none") return;
    if (which === "agents") {
      // An idle thread's Stop carries no correction: the composer's draft stays put.
      const id = currentId();
      if (id === null) return;
      try {
        const stopped = await stopThreadAgents(id);
        if (stopped.length > 0) toast.success("Agents stopped");
        else toast.info("No agents were running");
      } catch (err) {
        // A 409 means a run of the thread's own started meanwhile; its cancel is the stop.
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to stop the agents.",
        );
      } finally {
        subagents.refetch();
      }
      return;
    }
    await stream.cancel();
    if (!correction) return void toast.success("Run cancelled");
    toast.success("Run cancelled — sending your correction");
    // No attachment ids: attaching is unavailable while a run streams.
    // The composer cleared the correction on STOP, so a refusal restores it via the prefill.
    if (!(await sendTurn(correction, []))) stream.stashDraft(correction);
  };

  onMount(() => {
    // The sessions list is a singleton not refetched per mount; catch out-of-band changes.
    refreshSessions();
  });

  // Folder changes and the worktree branch, read once for every consumer; re-read when a turn settles.
  const threadChanges = createThreadChanges(currentId, () =>
    stream.sending() ? 0 : 1,
  );
  const branch = threadChanges.branch;
  const refetchChanges = (): void => {
    threadChanges.changes.refetch();
    branch.refetch();
  };

  // Re-read when a turn settles (a launch has likely just happened); polls itself while any agent works.
  const subagents = createSubagentsState(currentId, () =>
    stream.sending() ? 0 : 1,
  );

  const browser = createBrowserState(currentId, browserOpenedSeq);
  const [ownCallsign] = createResource(
    () => {
      const id = currentId();
      const parentId = currentSummary()?.parentId;
      return id && parentId ? ([id, parentId] as const) : undefined;
    },
    ([id, parentId]) => fetchOwnCallsign(id, parentId),
  );
  const speaker = (): string =>
    (currentSummary()?.parentId ? settled(ownCallsign) : undefined) ??
    agentDisplayName(threadAgentName(currentSummary()?.agentName));
  const viewport = useChatViewport(currentId, {
    ...stream,
    speaker,
    browser,
    branch: branch.latest,
    changes: threadChanges.changes.latest,
    refetchChanges,
    subagents: subagents.latest,
    refetchSubagents: subagents.refetch,
  });

  registerChatRoomKeymap({
    viewport,
    focusTranscript: () => transcript.element()?.focus(),
    startNew,
  });

  // The controller moves a new thread's draft across when it adopts its id, using this same key.
  const composerKey = () => chatDraftKey(currentId());

  // Attachments are transient: switching threads discards them. Not when the seat only
  // moves from null to the id the stream was already bound to (same thread, now named);
  // clearing then would drop files attached during the first turn.
  const attachments = createComposerAttachments();
  createEffect(
    on(currentId, (id, prev) => {
      if (isNamedInPlace(id, prev, stream.conversationId())) return;
      attachments.clear();
    }),
  );

  const rename = createRenameConversation({
    conversationId: currentId,
    currentTitle: () => currentSummary()?.title,
  });

  const actions = createConversationActions({
    conversationId: currentId,
    messages: stream.messages,
    speaker,
    sending: stream.sending,
    removeMessage: stream.removeMessage,
    browser,
    showBrowser: () => viewport.showSurface("browser"),
    onDeleted: clearThread,
    onForked: setCurrentId,
  });
  const titleWorking = () => stream.titlePending() || actions.retitling();

  const savedProjectLabel = (): string | undefined => {
    const summary = currentSummary();
    if (!summary?.projectId) return undefined;
    const project = settled(projects)?.projects.find(
      (p) => p.id === summary.projectId,
    );
    return project ? directoryLabel(project) : summary.workspace;
  };

  // Derived once for both menus: the `@` files and `/` commands must come from the same checkout.
  const composerProjectId = (): string | null =>
    currentSummary()?.projectId ??
    (currentId() === null ? stagedProjectId() : null) ??
    null;

  // Every `/` action reuses a relay this room already owns. `fork` takes the newest turn.
  const agents = useAgents(() => composerProjectId() ?? undefined);
  const agentValue = (): string =>
    agentChoice() ?? threadAgentName(currentSummary()?.agentName);

  const commands = createComposerCommands(currentId, composerProjectId, {
    compact: () => void stream.compactNow(),
    fork: () => {
      const last = stream.messages.at(-1);
      if (last) void actions.fork(last.id);
    },
    retitle: () => void actions.retitle(),
    newThread: clearThread,
    setPermissionLevel: (level) => {
      const parsed = parsePermissionLevel(level);
      if (parsed) setPermission(parsed);
      else toast.error(`"${level}" isn't a permission level.`);
    },
  });

  // The `@` menu is shut for a thread with no project.
  const fileRefs = createComposerFileRefs(composerProjectId, currentId);

  /** Both menus behind the Composer's one slot. Each answers only for its own trigger, so
   *  one is open at a time; merging here keeps the design system ignorant of `/` and `@`. */
  const composerMenu = {
    groups: () => [...commands.groups(), ...fileRefs.groups()],
    onQuery: (token: Parameters<typeof commands.onQuery>[0]) => {
      commands.onQuery(token);
      fileRefs.onQuery(token);
    },
    // Routed by id prefix, not fallthrough: an action command legitimately returns null
    // (it ran), which a fallthrough would misread as "not mine".
    onPick: (item: ComposerMenuItem) =>
      item.id.startsWith("file-")
        ? fileRefs.onPick(item)
        : commands.onPick(item),
    onClear: () => {
      commands.clear();
      fileRefs.clear();
    },
  };

  /** Sends after both staged sets are checked against the typed text. An action sends
   *  nothing to the model (a fully typed command counts too; `consume` reads it off the
   *  text). Resolves `false` when the backend refused the turn so the Composer restores
   *  the text and attachments; an action resolves `true`. */
  const sendTurn = (
    text: string,
    attachmentIds: string[],
  ): Promise<boolean> => {
    const intent = commands.consume(text);
    const refs = fileRefs.consume(text);
    if (intent.kind === "acted") return Promise.resolve(true);
    return stream.send(text, attachmentIds, {
      command: intent.command,
      fileRefs: refs,
    });
  };

  // ArrowUp in the empty composer edits the newest queued message; same hold protocol as
  // the bubble and the dock (see `composerRecall.ts`).
  const recall = createComposerRecall({
    items: () => stream.inbox().items,
    editQueued: (id, text) => void stream.editQueued(id, text),
    holdQueued: (id, held) => void stream.holdQueued(id, held),
    stash: stream.stashDraft,
  });
  // Leaving the thread releases the hold, or its run would stall on a message nobody edits.
  createEffect(on(currentId, () => recall.cancel(), { defer: true }));
  // A park releases it too: the dock replaces the composer and has its own editor, so a
  // second invisible hold would stall the run.
  createEffect(
    on(
      () => Boolean(stream.park()),
      (parked) => parked && recall.cancel(),
    ),
  );

  // The dock opens from the count chip, and on its own when a Stop leaves the inbox waiting.
  const inbox = stream.inbox;
  const [dockOpen, setDockOpen] = createSignal(false);
  createEffect(
    on(
      () => inbox().waiting,
      (waiting) => waiting && setDockOpen(true),
    ),
  );
  createEffect(() => {
    if (inbox().items.length === 0) setDockOpen(false);
  });
  const dockVisible = () => dockOpen() && inbox().items.length > 0;
  const inboxActions = {
    onEdit: (id: string, text: string) => void stream.editQueued(id, text),
    onWithdraw: (id: string) => void stream.withdrawQueued(id),
    onHold: (id: string, held: boolean) => void stream.holdQueued(id, held),
    onResume: () => void stream.resumeInbox(),
  };

  /** Jumps to the newest turn holding an ask (through the follow, so the bottom pin
   *  lets go), else to the tail. */
  const jumpToAsk = (): void => {
    const asking = stream.messages.findLast((m) =>
      m.blocks?.some((b) => b.kind === "approval" || b.kind === "question"),
    );
    const el = asking ? transcript.scrollToMessage(asking.id) : null;
    if (!el) transcript.jumpToLatest();
  };

  const status = createConversationStatus({
    conversationId: currentId,
    stats: stream.stats,
    usage: stream.usage,
    tasks: stream.tasks,
    grantsRevalidate: conversationGrantsRevision,
  });

  /** No-op while pending: the level in hand is a placeholder to step from. */
  const cyclePermission = () => {
    if (permissionPending()) return;
    setPermission(nextPermissionLevel(permission()));
  };

  /** Height of the title block over the transcript. Measured because it grows with the
   *  workspace hint and the recap. */
  const [overlayHeight, setOverlayHeight] = createSignal(0);
  const measureOverlay = (el: HTMLElement) => {
    const observer = new ResizeObserver(() =>
      setOverlayHeight(el.offsetHeight),
    );
    observer.observe(el);
    onCleanup(() => observer.disconnect());
  };

  return (
    <AllowHostContext.Provider value={allowHost}>
      <div ref={viewport.rowRef} class="flex h-full min-h-0">
        <section class="flex min-h-full min-w-0 flex-1 flex-col">
          {/* The title block overlays the transcript, which scrolls under it through a
            `ScrollFade`. Nothing in here (this wrapper, the overlay, the transcript's
            wrapper) may carry `opacity`, `filter`, `mask`, `isolation` or a `will-change`
            of them: each makes a backdrop root and the fade's blur samples nothing
            (see `.heph-glass`). */}
          <div class="relative flex min-h-0 flex-1 flex-col">
            <TranscriptView
              stream={stream}
              viewport={viewport}
              actions={actions}
              scroll={transcript}
              conversationId={currentId}
              history={history}
              onRetryHistory={reloadHistory}
              measure={MEASURE}
              insetTop={overlayHeight}
            />
            {/* After the transcript in the DOM and with no `z-*`: paint order is DOM order,
              and a stacking context here would be a backdrop root the fade can't survive. */}
            <div ref={measureOverlay} class="absolute inset-x-0 top-0">
              <ScrollFade edge="top" />
              <div class="relative">
                <ChatRoomHeader
                  title={headerTitle}
                  reveal={headerReveal}
                  workspaceHint={workspaceHint}
                  working={titleWorking}
                  conversationId={currentId}
                  messageCount={() => stream.messages.length}
                  compacting={stream.compacting}
                  turnLive={stream.sending}
                  viewport={viewport}
                  branch={branch.latest}
                  actions={{
                    rename: rename.open,
                    retitle: () => void actions.retitle(),
                    compact: () => void stream.compactNow(),
                    openBrowser: () => void actions.openBrowser(),
                    copy: actions.copyTranscript,
                    remove: () => void actions.removeConversation(),
                  }}
                />
                {/* Pinned under the title, not in the scroll. It takes an opaque fill because
                  prose scrolling through prose is unreadable at any blur; `flow-root`
                  keeps the panel's bottom margin inside. */}
                <div class="flow-root bg-bg">
                  <ThreadRecap
                    summary={recapSummary}
                    streaming={stream.sending}
                    onDismiss={() => setRecapDismissed(openId())}
                  />
                </div>
              </div>
            </div>
          </div>

          {/* The transcript dissolves into the dock through the same `ScrollFade` as the
            title. No top padding: the fade reaches over the transcript's last band, which
            the transcript keeps as its bottom inset; a pad would leave bare page between
            fade and frame. The background spans full width while contents take `MEASURE`.
            It is dropped while a run is parked, since the park panel is glass and an
            opaque fill leaves it nothing to frost (see `ParkDock`).
            `pb-4` is the whole bottom margin (the shell gives this route a flush bottom
            edge, `isFlushRoute`): it clears the composer's registration ticks, 6px
            outside its frame, with air to spare. */}
          <div
            class={cx("sticky bottom-0 px-4 pb-4", !stream.park() && "bg-bg")}
          >
            <ScrollFade edge="bottom" />
            <div class={cx("relative", MEASURE)}>
              {/* A parked run takes the composer's slot and offers only what it waits for. */}
              <Show when={stream.park()}>
                {(park) => (
                  <ParkDock
                    park={park()}
                    draft={stream.parkDraft()}
                    onDraft={stream.patchParkDraft}
                    // A compare pane passes nothing and the read-plan button is not offered.
                    onReadPlan={() => viewport.showSurface("plan")}
                    onStop={() => void stopRun()}
                    onSubmit={(settlement) =>
                      stream.resolvePark(park().messageId, settlement)
                    }
                    // A park and a queued message reach the model on the same resume, so the
                    // dock must also make the queue answerable.
                    inbox={inbox()}
                    inboxActions={inboxActions}
                  />
                )}
              </Show>
              <Show when={!stream.park()}>
                <Show when={dockVisible()}>
                  <InboxDock framed inbox={inbox()} {...inboxActions} />
                </Show>
                <Composer
                  autofocus
                  placeholder={`Message ${
                    agentChoice() === undefined
                      ? speaker()
                      : agentDisplayName(agentValue())
                  }…`}
                  streaming={stream.sending()}
                  stoppable={route() === "agents"}
                  queued={stream.sending() && stream.runQueue() !== null}
                  onStop={(correction) => void stopRun(correction)}
                  onSend={sendTurn}
                  menu={composerMenu}
                  recall={recall}
                  // The backend's context-window refusal, surfaced before the send.
                  sendBlocked={sendBlockedReason()}
                  attachments={attachments}
                  storageKey={composerKey()}
                  prefill={stream.undeliveredDraft()}
                  onPrefillConsumed={stream.clearUndeliveredDraft}
                  onShiftTab={cyclePermission}
                  headerStart={
                    <ComposerHeaderStatus
                      title={() => currentSummary()?.title ?? null}
                      streaming={stream.sending}
                      compacting={stream.compacting}
                      detached={stream.detached}
                      runClock={stream.runClock}
                      runQueue={stream.runQueue}
                      activity={() => currentSummary()?.activity}
                      lastOutcome={() => currentSummary()?.lastOutcome}
                    />
                  }
                  headerEnd={
                    <ComposerHeaderAside
                      streaming={stream.sending}
                      runClock={stream.runClock}
                      queued={() => inbox().items.length}
                      queuedOpen={dockVisible}
                      onToggleQueued={() => setDockOpen((open) => !open)}
                      branch={() => branch.latest()?.branch}
                    />
                  }
                  // The action tier's echo: a run the backend reports waiting whose ask
                  // this room isn't holding (a driven park uses the dock instead).
                  waiting={
                    !stream.park() &&
                    currentSummary()?.activity === "awaiting_input"
                      ? { label: "Waiting on you", onJump: jumpToAsk }
                      : null
                  }
                  controls={
                    // The project is chosen while staged and only named once saved, since
                    // re-pointing would strand the thread's worktree. The level is a live
                    // control available throughout and rides the next send.
                    <>
                      <ModelPicker />
                      <PermissionControl
                        level={permission()}
                        onLevelChange={setPermission}
                        // While an opened thread's level is in flight the seat holds the
                        // strictest as a placeholder; the control reports that.
                        pending={permissionPending()}
                      />
                      <AgentControl
                        agents={settled(agents) ?? []}
                        value={agentValue()}
                        onChange={setAgentChoice}
                      />
                      <Show
                        when={openId() === null}
                        fallback={
                          <Show when={savedProjectLabel()}>
                            {(label) => (
                              <StatusCell class="max-sm:hidden">
                                {label()}
                              </StatusCell>
                            )}
                          </Show>
                        }
                      >
                        <ProjectControl
                          projects={settled(projects)?.projects ?? []}
                          value={stagedProjectId()}
                          onChange={setStagedProjectId}
                        />
                      </Show>
                    </>
                  }
                  trailing={status.items()}
                />
              </Show>
              {/* While parked, the composer's status bar is replaced by a boxed one so the
                trailing cells stay visible. `Disconnected`, normally in the composer
                header, takes its leading cell: one place at a time. The fill is its own
                (shared with the task rows) because the wrapper's is dropped during a park. */}
              <div class="bg-bg">
                <Show when={stream.park()}>
                  <StatusBar
                    edge="box"
                    start={
                      <Show when={stream.detached()}>
                        <StatusCell class="text-alert">Disconnected</StatusCell>
                      </Show>
                    }
                    end={status.items()}
                    class="mt-1.5"
                  />
                </Show>
                {status.taskRows()}
              </div>
            </div>
          </div>
        </section>

        <ChatViewportMounts viewport={viewport} />

        {rename.element}
      </div>
    </AllowHostContext.Provider>
  );
}
