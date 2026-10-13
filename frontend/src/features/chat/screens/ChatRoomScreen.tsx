import {
  Show,
  createEffect,
  createMemo,
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
import { threadAgentName } from "~/features/agents/model";
import { ThreadRecap } from "../components/ThreadRecap";
import { TranscriptView } from "../components/TranscriptView";
import { ModelPicker } from "~/app/ModelPicker";
import { createBrowserState } from "../browserState";
import { createConversationActions } from "../conversationActions";
import { registerChatRoomKeymap } from "../chatRoomKeymap";
import { useChatViewport } from "../useChatViewport";
import { createBranchState } from "../branchState";
import { createSubagentsState } from "../subagentsState";
import { createTranscriptFollow } from "../transcriptScroll";
import { createRenameConversation } from "../components/RenameConversationModal";
import { createComposerCommands } from "../commands/useComposerCommands";
import { createComposerFileRefs } from "../files/useFileRefs";
import { createComposerRecall, isOperatorQueued } from "../composerRecall";
import { nextPermissionLevel, parsePermissionLevel } from "../model";

/** The conversation's reading measure. A line of text on a 27" display is
 *  unreadable at full width long before it is uncomfortable, and the composer
 *  spanning the whole bottom of the screen made the input read as a page
 *  element rather than as a thing to type into.
 *
 *  It is deliberately ONE constant applied to both the transcript's content and
 *  the composer dock. They are the same column and must agree: an input
 *  narrower than the messages above it looks like a mistake, and an input wider
 *  than them looks like two layouts. The scroll container and the dock's
 *  background still span the full width — only their contents are centred — so
 *  the scrollbar stays at the edge of the pane and the transcript still
 *  disappears behind the dock rather than beside it. */
const MEASURE = "mx-auto w-full max-w-4xl";

/** Chat room: a searchable thread rail and a live streaming conversation. On
 *  entry it resumes the last conversation only while it's warm (recency-gated),
 *  otherwise it opens a fresh composer — the overview launchpad can also hand it
 *  a thread to open or a message to start. */
export function ChatRoomScreen(): JSX.Element {
  const sessions = useChatSessions();
  // The room's stream, selected conversation (null = new, unsaved), and loaded
  // history live in a persistent module-level controller — not in this component
  // — so navigating away mid-turn and back doesn't tear down the in-flight run.
  const {
    currentId,
    openId,
    setCurrentId,
    history,
    reloadHistory,
    stream,
    warmResolved,
    markWarmResolved,
    permission,
    setPermission,
    permissionPending,
    agentChoice,
    setAgentChoice,
  } = mainChat();
  const projects = useProjects();

  /** The project a **staged** thread would be scoped to — undefined once the thread is
   *  saved. Read by the header's subtitle and the composer's picker, so the two can't
   *  disagree about which project is in play.
   *
   *  Staged means *no backend id anywhere*, which is `openId` and not `currentId`: the
   *  room keeps its seat empty for the length of a new thread's first turn, and reading
   *  that as "still staged" put the hint about the name this thread does not have yet
   *  directly under the name the backend had just given it. */
  const stagedProject = createMemo(() => {
    if (openId() !== null) return undefined;
    const id = stagedProjectId();
    return id
      ? settled(projects)?.projects.find((p) => p.id === id)
      : undefined;
  });

  // Follow the stream: pinned to the bottom while the answer arrives, yielding the
  // moment the operator scrolls up to read back (see `transcriptScroll.ts`).
  const transcript = createTranscriptFollow({
    messages: stream.messages,
    sending: stream.sending,
    conversationId: currentId,
  });

  // Header reflects the thread on screen (messages resolve through the seam).
  //
  // `openId`, not `currentId`: during a new thread's first turn the room has not
  // seated itself on the backend's id yet, and a header keyed on the seat has
  // nothing to resolve — which is why an auto-generated name, emitted seconds into
  // the run, used to appear only once the stream ended.
  const currentSummary = createMemo(() => {
    const id = openId();
    return id ? sessions()?.find((s) => s.id === id) : undefined;
  });
  const headerTitle = () => currentSummary()?.title ?? "New conversation";

  /** Which thread the operator has put the recap away for. View state and nothing
   *  else — it is a toggle over a band, so it stays here rather than in storage the
   *  backend would have to own. Keyed by thread so dismissing one doesn't dismiss the
   *  next one the operator opens, which is the whole case for the band. */
  const [recapDismissed, setRecapDismissed] = createSignal<string | null>(null);
  const recapSummary = createMemo(() =>
    recapDismissed() === openId() ? undefined : currentSummary(),
  );
  // A just-generated title for the open thread, if the backend named it this turn.
  const headerReveal = () => {
    const id = openId();
    return id ? titleReveals[id] : undefined;
  };

  // Resolve the entry intent: new-from-overview › open-specific › recency.
  createEffect(() => {
    // Explicit cross-surface intents (the overview launchpad) are deliberate and
    // apply on every entry — even after the one-time warm resume below.
    const draft = consumePendingDraft();
    if (draft) {
      // Only adopt an explicit pick — an empty draft (discovery not yet resolved
      // on the overview) must not clobber the operator's sticky selection.
      if (draft.model) void setSelectedModel(draft.model);
      // A `/level` picked on the launchpad, applied before the thread is created rather
      // than after — the same relay the room's own control calls, so the level the
      // operator chose is the one this first turn actually runs at.
      if (draft.permissionLevel) setPermission(draft.permissionLevel);
      setCurrentId(null);
      queueMicrotask(
        () =>
          void stream.send(draft.text, draft.attachmentIds, {
            // Whatever `/` command the launchpad staged. Without this the room would
            // send the operator's literal `/reviewer …` with nothing saying what that
            // token was, and the turn would quietly mean less than it said.
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
    // Recency resume is a once-per-session entry concern. The room's state is now
    // persistent across navigation, so re-running it on every remount would yank
    // the operator off whatever thread (or fresh composer) they'd left open.
    if (warmResolved()) return;
    const list = sessions();
    if (!list) return; // wait for the seam to resolve
    if (untrack(currentId) === null) setCurrentId(entrySessionId(list));
    markWarmResolved();
  });

  /** The project a staged thread is scoped to, as the header names it. Nothing for a
   *  saved thread — its branch chip is the answer there. */
  const workspaceHint = (): string | undefined => {
    const project = stagedProject();
    return project ? directoryLabel(project) : undefined;
  };

  /** Off the open thread and onto a fresh composer. Unconditional — this is where the
   *  room goes when the thread it was showing is *gone*, so it can never decline. */
  const clearThread = () => setCurrentId(null);

  /** A fresh composer, **asked for**. It stays in the project of the thread the operator
   *  is standing in, since that is the work they are most likely continuing; from a
   *  composer that is already staged it changes nothing. */
  const startNew = () => {
    if (openId() !== null) setStagedProjectId(currentSummary()?.projectId);
    clearThread();
  };

  // Stop the live run for real: cancel on the backend, abort the local stream.
  // `cancel()` surfaces its own backend error; this only adds the success note.
  //
  // **A correction typed into the composer rides the same act.** Interrupting is rarely
  // the whole intention — the run is stopped because it is doing the wrong thing, and
  // what comes next is saying what the right thing was. As two acts that is a stop, a
  // pause while the composer comes back, and a send; and in that gap the correction is
  // retyped, or a queued message restoring itself lands on top of it.
  //
  // Nothing about the stopped run is discarded to make room for it: the cancel leaves
  // the turn where it stopped — its answer so far, every tool block, every command's
  // output — and the correction opens the next turn against that history, which is what
  // makes it a correction rather than a restart.
  const stopRun = async (correction?: string) => {
    if (!stream.sending()) return;
    await stream.cancel();
    if (!correction) return void toast.success("Run cancelled");
    toast.success("Run cancelled — sending your correction");
    // No attachment ids: attaching is unavailable while a run streams, so there can be
    // nothing ready to carry.
    // The composer cleared the correction when STOP took it, so a refusal here would
    // otherwise lose it: it goes back through the prefill the composer already reads.
    if (!(await sendTurn(correction, []))) stream.stashDraft(correction);
  };

  onMount(() => {
    // The sessions list is an app-wide singleton resource (no longer refetched
    // per mount), so pull once on entry to catch any out-of-band changes — a
    // second tab, a scheduled agent — since it was last loaded.
    refreshSessions();
  });

  // A worktree thread's branch, read once for everyone who shows it: the header's chip and
  // the Diff surface are two views of one fetch, and two resources over the same
  // endpoint would disagree for as long as either was in flight. Re-reads when a turn
  // settles, since that is when the agent has just changed something.
  const branch = createBranchState(
    currentId,
    () => (stream.sending() ? 0 : 1),
    () => currentSummary()?.workspaceKind,
  );

  // The thread's sub-agents. Re-read when a turn settles for the same reason as the
  // branch — that is the moment a launch has most likely just happened — and it keeps
  // itself current on its own while any of them is still working.
  const subagents = createSubagentsState(currentId, () =>
    stream.sending() ? 0 : 1,
  );

  // The viewport pane beside the conversation — everything about what it holds, how
  // wide it is, whether it renders as an aside or a sheet, and where focus goes when
  // it closes. It is a concern of its own, so it lives in one.
  const browser = createBrowserState(currentId);
  const viewport = useChatViewport(currentId, {
    ...stream,
    browser,
    branch: branch.latest,
    refetchBranch: branch.refetch,
    subagents: subagents.latest,
    refetchSubagents: subagents.refetch,
  });

  registerChatRoomKeymap({
    viewport,
    focusTranscript: () => transcript.element()?.focus(),
    startNew,
  });

  // Per-conversation draft key, so an unsent message is restored on return. The
  // controller moves a new thread's draft across when it adopts its id, keyed by the
  // same derivation.
  const composerKey = () => chatDraftKey(currentId());

  // File attachments for the next turn. Transient (not persisted like the draft):
  // switching threads discards any still-attached files so they don't ride along
  // to a different conversation.
  //
  // **Except when the switch is not a switch.** A new thread's seat moves from null to
  // its backend id when its first run ends, and that id is the one the stream has been
  // bound to all along — the same thread, now named. Clearing there would drop whatever
  // the operator attached while the first turn ran, on a thread they never left. Read
  // off the stream's own binding rather than tracked as a flag, because it is exactly
  // the fact in question: the thread on screen before the flip is the one after it.
  const attachments = createComposerAttachments();
  createEffect(
    on(currentId, (id, prev) => {
      if (prev === null && id !== null && id === stream.conversationId())
        return;
      attachments.clear();
    }),
  );

  const rename = createRenameConversation({
    conversationId: currentId,
    currentTitle: () => currentSummary()?.title,
  });

  // Retitle, fork, copy, the thread's browser and the two deletes — everything that
  // acts on the thread rather than on a turn in it.
  const actions = createConversationActions({
    conversationId: currentId,
    messages: stream.messages,
    sending: stream.sending,
    cancel: stream.cancel,
    removeMessage: stream.removeMessage,
    browser,
    showBrowser: () => viewport.showSurface("browser"),
    onDeleted: clearThread,
    onForked: setCurrentId,
  });
  // A "working" throbber sits on the title while the backend names the thread —
  // either the first-turn auto-title (stream) or a manual regenerate.
  const titleWorking = () => stream.titlePending() || actions.retitling();

  /** The saved thread's project, named for the status bar. */
  const savedProjectLabel = (): string | undefined => {
    const summary = currentSummary();
    if (!summary?.projectId) return undefined;
    const project = settled(projects)?.projects.find(
      (p) => p.id === summary.projectId,
    );
    return project ? directoryLabel(project) : summary.workspace;
  };

  // Which project this composer is about, for both menus. The saved thread's own, else
  // the one staged for the next thread — the same pair the header subtitle already
  // reads, so neither picker can be browsing a project the thread is not about to be
  // scoped to.
  //
  // Derived once and passed to both rather than resolved twice: the `@` picker lists that
  // checkout's files and the `/` picker offers the commands that checkout declares, and a
  // pair that disagreed would put a file from one tree beside a command from another.
  const composerProjectId = (): string | null =>
    currentSummary()?.projectId ??
    (currentId() === null ? stagedProjectId() : null) ??
    null;

  // The composer's `/` menu. Every action it can fire is a relay this room already owns
  // — a command is a second way to reach a control, never a second implementation of
  // one. `fork` takes the newest turn, which is what "fork from here" means when the
  // operator is typing rather than pointing at a message.
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

  // The `@` menu, over the project's filesystem. Shut for a thread with no project — its
  // container holds nothing the operator has ever seen.
  const fileRefs = createComposerFileRefs(composerProjectId, currentId);

  /** Both menus behind the Composer's one slot.
   *
   *  The Composer reads the token and reports it to both; each answers for its own
   *  trigger and returns nothing for the other, so exactly one is ever open. Merging them
   *  here rather than teaching the Composer about two controllers keeps the design system
   *  ignorant of what a `/` and an `@` mean, which is the whole point of the seam. */
  const composerMenu = {
    groups: () => [...commands.groups(), ...fileRefs.groups()],
    onQuery: (token: Parameters<typeof commands.onQuery>[0]) => {
      commands.onQuery(token);
      fileRefs.onQuery(token);
    },
    // Routed by the row's own id prefix rather than by trying one and falling through:
    // an action command legitimately returns null (it ran, and the field clears), and a
    // fallthrough would read that as "not mine" and ask the file picker about it.
    onPick: (item: ComposerMenuItem) =>
      item.id.startsWith("file-")
        ? fileRefs.onPick(item)
        : commands.onPick(item),
    onClear: () => {
      commands.clear();
      fileRefs.clear();
    },
  };

  /** Send, once both staged sets have been read against what was actually typed.
   *
   *  An action resolves to nothing sent: the relay has already run (or just ran, for the
   *  one that carries an argument), and there is no message for the model. A command
   *  the operator typed in full rather than picked counts the same — `consume` falls
   *  back to reading it off the text.
   *
   *  Returns the stream's verdict to the Composer: `false` means the backend did not
   *  take the turn, and the Composer puts the text and attachments back. An action is
   *  `true` — it did what it was sent to do. */
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

  // ArrowUp in the empty composer edits the newest queued message in place — the third
  // surface that edits one, following the same hold-and-draft protocol as the bubble and
  // the dock's list (see `composerRecall.ts`).
  const recall = createComposerRecall({
    messages: stream.messages,
    editQueued: (id, text) => void stream.editQueued(id, text),
    holdQueued: (id, held) => void stream.holdQueued(id, held),
    stash: stream.stashDraft,
  });
  // A recall belongs to the thread it was opened in. Leaving it releases the hold, or the
  // run behind the thread just left would stall on a message nobody is editing any more.
  createEffect(on(currentId, () => recall.cancel(), { defer: true }));
  // So does a park: the dock takes the composer's slot, which is the surface being
  // unmounted under the edit — and the dock lists the same message with its own editor,
  // where a second, invisible hold would stall it with nothing on screen to say why.
  createEffect(
    on(
      () => Boolean(stream.park()),
      (parked) => parked && recall.cancel(),
    ),
  );

  /** The operator's own messages queued into the live run. Read once for both places
   *  that show them — the composer's header counts them, the park dock lists them — so
   *  the two can never disagree about how many are waiting, and the recall walks the
   *  same set through the same predicate. */
  const queuedOperator = createMemo(() =>
    stream.messages.filter(isOperatorQueued),
  );

  /** The composer's `Jump ↑`: to the newest turn holding an ask, through the follow so
   *  the bottom pin lets go — else to the tail, where the run is. */
  const jumpToAsk = (): void => {
    const asking = stream.messages.findLast((m) =>
      m.blocks?.some((b) => b.kind === "approval" || b.kind === "question"),
    );
    const el = asking ? transcript.scrollToMessage(asking.id) : null;
    if (!el) transcript.jumpToLatest();
  };

  // The composer's status bar: its trailing cells, and the task rows they disclose.
  const status = createConversationStatus({
    conversationId: currentId,
    stats: stream.stats,
    usage: stream.usage,
    tasks: stream.tasks,
    grantsRevalidate: conversationGrantsRevision,
  });

  /** Shift+Tab's next level — a no-op while the control is pending, for the same reason
   *  the control itself is disabled then: the level in hand is a placeholder, and
   *  stepping from it would write a level chosen relative to nothing. */
  const cyclePermission = () => {
    if (permissionPending()) return;
    setPermission(nextPermissionLevel(permission()));
  };

  /** How tall the title block laid over the transcript stands, so the transcript can
   *  start its first turn beneath it. Measured rather than assumed: the block grows a
   *  line for a staged thread's workspace hint and a whole panel for the recap, and a
   *  fixed spacer would either hide the first turn under one of them or leave a gap
   *  where neither is showing. */
  const [overlayHeight, setOverlayHeight] = createSignal(0);
  const measureOverlay = (el: HTMLElement) => {
    const observer = new ResizeObserver(() =>
      setOverlayHeight(el.offsetHeight),
    );
    observer.observe(el);
    onCleanup(() => observer.disconnect());
  };

  return (
    <div ref={viewport.rowRef} class="flex h-full min-h-0">
      {/* Conversation — the thread list now lives in the app rail's RECENTS, so
          the body is free for the conversation plus the viewport pane. */}
      <section class="flex min-h-full min-w-0 flex-1 flex-col">
        {/* The transcript runs the full height of this box, and the title block is
            laid over its top rather than stacked above it: the conversation scrolls
            out of sight *under* the title, through a `ScrollFade`, instead of being
            cut at the header's bottom edge by a band that spent its own height.

            Nothing in here — this wrapper, the overlay, the transcript's own wrapper —
            may carry `opacity`, `filter`, `mask`, `isolation` or a `will-change` of
            them: any one makes a backdrop root, and the fade's blur would sample
            nothing (the trap noted on `.heph-glass`). */}
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
          {/* AFTER the transcript in the DOM and with no `z-*`: both are positioned,
              so paint order is DOM order and this stays on top without a stacking
              context — which some engines treat as a backdrop root, the one thing the
              fade inside it cannot survive. The session menu's popover portals to
              the body, so tab order is the only thing the position costs. */}
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
              {/* Pinned under the title, not folded into the transcript: a recap
                  inside the scroll is a message that is not one, sitting at the top of
                  a thread the operator has just been dropped at the bottom of. It
                  carries the ground as a fill because it is a paragraph — the fade is
                  for a line of title, and prose scrolling through prose is unreadable
                  at any blur. `flow-root` keeps the panel's bottom margin inside it. */}
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

        {/* The composer docks on the page background, and the transcript dissolves
            into it through the same `ScrollFade` the title uses at the top — one
            edge treatment for both ends of the conversation, the composer's own
            opaque fill and ruled frame doing the separating.
            **No top padding**: the fade reaches up over the transcript's last band
            on its own, and the transcript keeps exactly that band as its bottom
            inset, so the last turn at rest clears it. A pad here would add a strip
            of bare page between the fade and the frame. */}
        {/* The dock's own background spans the full width — it is what the
            transcript scrolls out of sight behind — while its contents take the
            same measure as the transcript above. */}
        {/* The background is dropped while a run is parked, and only then. The park
            panel is *glass*: an opaque fill behind it leaves it nothing to frost, and
            it would read as a flat tinted card rather than as the transcript seen
            through it (see `ParkDock`). It stays full-width for the composer, which
            needs the transcript to disappear behind it rather than beside it. */}
        {/* `pb-4` is the whole bottom margin: the shell gives this route its bottom
            edge flush (`isFlushRoute`). It has to clear the composer's registration
            ticks, which sit 6px outside its frame (the shell's scroll root would clip
            a pair left hanging past it), and then leave a little air — at `pb-2` the
            ticks cleared but the unit read as resting on the window's edge. */}
        <div class={cx("sticky bottom-0 px-4 pb-4", !stream.park() && "bg-bg")}>
          <ScrollFade edge="bottom" />
          <div class={cx("relative", MEASURE)}>
            {/* A parked run takes the composer's place. It is the same slot, so
                nothing below the transcript moves — but the only thing offered is
                the thing the run is waiting for. */}
            <Show when={stream.park()}>
              {(park) => (
                <ParkDock
                  park={park()}
                  draft={stream.parkDraft()}
                  onDraft={stream.patchParkDraft}
                  // This room has a Plan panel, so a plan the dock is asking about
                  // can be read at a panel's width one click away. A compare pane
                  // passes nothing and the button is not offered there.
                  onReadPlan={() => viewport.showSurface("plan")}
                  onStop={() => void stopRun()}
                  onSubmit={(settlement) =>
                    stream.resolvePark(park().messageId, settlement)
                  }
                  // A park and a queued message can be outstanding together, and both
                  // reach the model on the same resume — so the dock that has taken the
                  // composer's slot also has to be where the queue is answerable.
                  queued={queuedOperator()}
                  onEditQueued={(id, text) => void stream.editQueued(id, text)}
                  onWithdrawQueued={(id) => void stream.withdrawQueued(id)}
                  onHoldQueued={(id, held) => void stream.holdQueued(id, held)}
                />
              )}
            </Show>
            <Show when={!stream.park()}>
              <Composer
                autofocus
                streaming={stream.sending()}
                queued={stream.sending() && stream.runQueue() !== null}
                onStop={(correction) => void stopRun(correction)}
                onSend={sendTurn}
                menu={composerMenu}
                recall={recall}
                // The backend refuses a turn it can't keep inside a context window; this
                // is the same stop, arriving before the message is committed to it.
                sendBlocked={sendBlockedReason()}
                attachments={attachments}
                storageKey={composerKey()}
                prefill={stream.undeliveredDraft()}
                onPrefillConsumed={stream.clearUndeliveredDraft}
                onShiftTab={cyclePermission}
                headerStart={
                  // What the thread is doing: its name and `ready` at rest, the live
                  // run, a lost stream, or a run that ended in a way worth saying.
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
                  // The run's clock, queued messages waiting on the operator's say-so,
                  // or the branch a code thread writes to.
                  <ComposerHeaderAside
                    streaming={stream.sending}
                    runClock={stream.runClock}
                    queued={() => queuedOperator().length}
                    branch={() => branch.latest()?.branch}
                  />
                }
                // The action tier's echo. The dock takes the composer's slot for a
                // park this room is driving, so what reaches here is a run the backend
                // reports waiting that this room is not holding the ask for.
                waiting={
                  !stream.park() &&
                  currentSummary()?.activity === "awaiting_input"
                    ? { label: "Waiting on you", onJump: jumpToAsk }
                    : null
                }
                controls={
                  // The project is chosen while the thread is staged and only named once
                  // it is saved — it is set at creation, and re-pointing a thread would
                  // strand whatever worktree it had cut. The level is the opposite: it
                  // is the operator's live control over a thread already in flight, so
                  // it is offered at every moment of one, and it rides the next send.
                  <>
                    <ModelPicker />
                    <PermissionControl
                      level={permission()}
                      onLevelChange={setPermission}
                      // While an opened thread's own level is still in flight the seat
                      // holds the strictest one as a placeholder; the control reports
                      // that rather than naming a level the thread may not be at.
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
                // What the thread is doing: the task count, the stats behind their
                // trigger, and how full the context window is.
                trailing={status.items()}
              />
            </Show>
            {/* While a run is parked the Composer — and the status bar joined to it —
                gives its slot to the dock, but a disconnected stream and the task count
                are no less true for it; the bar's trailing cells stand on their own
                until the dock is gone, as the same bar boxed on all four sides. The
                composer's header line is gone with it, so `Disconnected` — said there
                the rest of the time — takes the bar's leading cell for the length of
                the park: one place at a time, never both.
                Its own fill, shared with the task rows: the wrapper's is gone while a
                park is up, and neither is part of the glass — without it they would
                sit on the transcript scrolling behind them. */}
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

      {/* Documents / live previews / artifacts sit beside the conversation, on a
          draggable divider above `lg` and in a full-screen sheet below it. */}
      <ChatViewportMounts viewport={viewport} />

      {rename.element}
    </div>
  );
}
