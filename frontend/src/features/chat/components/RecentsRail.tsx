import {
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  type JSX,
} from "solid-js";
import { useLocation, useNavigate } from "@solidjs/router";
import {
  ContextMenu,
  Icon,
  ListGroupHeader,
  StatusBar,
  StatusCell,
  StatusField,
  Tooltip,
  confirm,
  createContextMenu,
  toast,
  type MenuItem,
} from "~/ui";
import { registerKeymap } from "~/lib/keymap";
import { useNotifications } from "~/lib/stores/notifications";
import {
  activeProjectId,
  deleteProject,
  updateProject,
  useProjects,
  type Project,
} from "~/lib/stores/projects";
import {
  setStagedProjectId,
  stagedProjectId,
} from "~/lib/stores/stagedProject";
import {
  isPinned,
  mainChat,
  refreshSessions,
  togglePin,
  useChatSessions,
} from "../data";
import {
  deleteConversationFlow,
  retitleConversation,
} from "../conversationActions";
import { createRenameConversation } from "./RenameConversationModal";
import { directoryLabel, useAddDirectory } from "../addDirectory";
import { settled } from "~/lib/resource";
import type { ChatActivity } from "../model";
import type { WorkspaceDirectory } from "../sessionGroups";
import { SessionList, type WorkspaceStatus } from "./SessionList";

/** How often to re-read the list while a thread is working. A run in a thread the
 *  operator navigated away from ends server-side with nothing to tell this client,
 *  so the activity edges would otherwise stay lit until the next turn. */
const ACTIVITY_POLL_MS = 3000;

/** RECENTS — the conversation list, and the body of the nav rail itself. It was
 *  hoisted out of the chat page so the chat body is free for the conversation
 *  plus a viewport pane, and it is now what the rail *is*: mounted on every
 *  route, not only `/chat`, because threads are the work and everything that was
 *  configuration went to the settings dialog. It owns its chat-seam wiring (the shared sessions resource + the
 *  persistent room controller), so the app shell only positions it — no chat
 *  state or logic leaks into the shell. Selecting a thread drives the same
 *  `currentId` the room renders; opening from off the chat route navigates there
 *  first.
 *
 *  **The header has two buttons that make different things.** NEW starts a thread with
 *  no project; ADD DIRECTORY points the app at a directory and stages it as the project
 *  of the next thread. A thread is started in a project from the project's own section
 *  below, or by choosing it in the composer. They do not share a glyph: a plus here and
 *  on the section's row, a folder for the directory. */
export function RecentsRail(props: {
  /** The project filter, at the right end of the list's heading. The shell's to
   *  supply — the project scope is app-wide, not chat's. */
  filter?: JSX.Element;
}): JSX.Element {
  const sessions = useChatSessions();
  const projects = useProjects();
  const { currentId, openId, setCurrentId, stream } = mainChat();
  const location = useLocation();
  const navigate = useNavigate();
  const addDirectory = useAddDirectory();

  /** The rows to render: the server's list, plus an echo of the run this client
   *  is already streaming.
   *
   *  Activity is server-derived, and the refresh that fires when a turn starts
   *  races the backend recording the run — so the read comes back saying nothing
   *  is active. That alone would be a brief miss, except the poll below is gated
   *  on the list showing activity, so nothing was left to discover it: the row
   *  stayed dark for the whole run and only lit after a reload. A list that has
   *  to already know something is running in order to find out that something is
   *  running can never light the first one.
   *
   *  So echo what this client plainly knows about its own stream. This is a
   *  presentation echo, not a decision — the next poll replaces it with the
   *  server's answer, and a run started on another device still arrives the
   *  normal way. */
  const rows = createMemo(() => {
    const list = sessions();
    // `openId`, not `currentId`: a new thread's first turn is not seated until it ends,
    // so keyed on the seat its row stayed dark for the whole of that turn.
    const id = openId();
    if (!list || !id || !stream.sending()) return list;
    const echo: ChatActivity = stream.awaitingInput()
      ? "awaiting_input"
      : "running";
    return list.map((s) =>
      s.id === id && !s.activity ? { ...s, activity: echo } : s,
    );
  });

  /** The projects threads file under, or undefined while the listing is in flight — the
   *  list treats the two differently, and must.
   *
   *  Scoped the way the backend scopes the conversations listing: with a project
   *  active it returns that project's threads and the unfiled ones and nothing else, so
   *  a rail drawing every directory would draw headings whose threads are being
   *  filtered away underneath them. Archived projects drop out of the listing itself. */
  const visible = createMemo((): Project[] | undefined => {
    const dto = settled(projects);
    // Undefined means *still loading*, and the list holds its loading arm on it. An
    // errored resource never populates `latest`, so returning undefined there would
    // hold that arm forever — a rail stuck on LOADING with no error, no retry and no
    // way to reach ADD DIRECTORY. A failed read is no directories, which at least
    // leaves the operator somewhere they can act.
    if (!dto) return projects.error ? [] : undefined;
    // `activeProjectId` already answers null while unscoped; asking twice would leave
    // two copies of the scope rule to keep in agreement.
    const active = activeProjectId();
    return active ? dto.projects.filter((p) => p.id === active) : dto.projects;
  });

  const directories = createMemo(
    (): readonly WorkspaceDirectory[] | undefined =>
      visible()?.map((p) => ({ id: p.id, label: directoryLabel(p) })),
  );

  const byId = createMemo(
    () => new Map((visible() ?? []).map((p) => [p.id, p])),
  );
  const status = (projectId: string): WorkspaceStatus | undefined => {
    const repo = byId().get(projectId)?.repo;
    if (!repo) return undefined;
    if (!repo.isGitRepo) return { notARepo: true };
    return repo.uncommittedChanges
      ? { uncommitted: repo.uncommittedChanges }
      : undefined;
  };

  // Poll only while something is actually running — an idle rail makes no
  // requests, and the poll stops on its own once the last edge clears. Read off
  // `rows`, so the echo above opens the gate too: that is what lets the poll
  // correct and then clear the row it lit optimistically.
  const anyActive = createMemo(() => rows()?.some((s) => s.activity) ?? false);
  createEffect(() => {
    if (!anyActive()) return;
    // Nobody is looking at a hidden tab's rail, so it asks nothing; coming back to the
    // tab reads once straight away rather than waiting out the interval.
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refreshSessions();
    }, ACTIVITY_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") refreshSessions();
    };
    document.addEventListener("visibilitychange", onVisible);
    onCleanup(() => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    });
  });

  const toChat = () => {
    if (location.pathname !== "/chat") navigate("/chat");
  };
  const select = (id: string) => {
    setCurrentId(id);
    toChat();
  };
  const startNew = (projectId?: string) => {
    setStagedProjectId(projectId);
    setCurrentId(null);
    toChat();
  };

  /* ── The per-thread actions menu ────────────────────────────────────────────
   *
   * One instance for the whole list. Every row drives it — a right-click at the
   * cursor, or its own "···" against the button — and the items are rebuilt for
   * whichever row opened it. A menu per row would mount one portal, one backdrop and
   * one Escape listener per thread in the history.
   *
   * Its sibling below (`actions`) is the *directory* menu, and the two stay apart on
   * purpose: one acts on a project, this one on a conversation. */
  const threadMenu = createContextMenu();

  // The thread a rename is for, captured when the item is chosen. It cannot be read
  // from the menu at dialog time: selecting an item closes the menu first, so the open
  // key is already null by the time the modal asks.
  const [renameTarget, setRenameTarget] = createSignal<{
    id: string;
    title: string;
  } | null>(null);
  const rename = createRenameConversation({
    conversationId: () => renameTarget()?.id ?? null,
    currentTitle: () => renameTarget()?.title,
  });

  /** What a thread's own menu offers, built per opening against the row that opened it.
   *
   *  Deliberately not the room header's full set: COPY CONVERSATION, FORK and COMPACT
   *  all need the thread's messages, and the rail holds only summaries — offering them
   *  here would mean loading a thread the operator did not ask to open, to serve a menu
   *  they may dismiss. What is left acts on the thread by id alone. */
  const threadActions = (): MenuItem[] => {
    const id = threadMenu.openKey();
    if (!id) return [];
    // Read once and closed over: these run after the menu has closed.
    const row = rows()?.find((s) => s.id === id);
    const title = row?.title ?? "";
    const pinned = isPinned(id);
    return [
      {
        label: pinned ? "Unpin thread" : "Pin thread",
        icon: "pin",
        onSelect: () => togglePin(id),
      },
      {
        label: "Rename conversation",
        icon: "edit",
        onSelect: () => {
          setRenameTarget({ id, title });
          rename.open();
        },
      },
      {
        label: "Regenerate title",
        icon: "refresh",
        // No refresh of our own — `regenerateTitle` already re-reads the list.
        onSelect: () => void retitleConversation(id),
      },
      {
        label: "Delete conversation",
        icon: "trash",
        danger: true,
        onSelect: () => {
          void deleteConversationFlow(id, {
            // The same guard the room's menu applies, and it has to be here too: this
            // menu can delete the thread the operator is *currently streaming*, and
            // aborting the local SSE would not stop the run server-side — it would
            // keep generating into a conversation that no longer exists. Scoped to
            // that thread, so deleting any other row never touches a run in flight.
            beforeDelete: async () => {
              if (id === currentId() && stream.sending()) await stream.cancel();
            },
          }).then((deleted) => {
            // Only the open thread's disappearance restages the composer. Deleting
            // some other row must leave the operator where they were — that is the
            // whole point of being able to act on a thread without opening it.
            // (`deleteConversation` re-reads the list itself.)
            if (deleted && currentId() === id) setCurrentId(null);
          });
        },
      },
    ];
  };

  /** What a directory's own menu offers. Everything here acts on the *project*, which
   *  is why the list does not build it: filing and unfiling a directory is the projects
   *  seam's business, and the rail is only where the operator happens to be standing. */
  const actions = (projectId: string): MenuItem[] => {
    const project = byId().get(projectId);
    const name = project?.name ?? "this directory";
    return [
      {
        label: "Archive directory",
        icon: "archive",
        onSelect: () => void archive(projectId, name),
      },
      {
        label: "Delete directory",
        icon: "trash",
        danger: true,
        onSelect: () => void remove(projectId, name),
      },
    ];
  };

  /** Archiving takes the directory *and its threads* out of the rail. Said plainly
   *  before it happens, because the threads are the part that surprises: they are not
   *  deleted and they are not loose in the list — they come back with the directory. */
  const archive = async (projectId: string, name: string) => {
    const held = rows()?.filter((s) => s.projectId === projectId).length ?? 0;
    const ok = await confirm({
      title: `Archive ${name}?`,
      detail: held
        ? `Its ${held} thread${held === 1 ? "" : "s"} leave the rail with it. Nothing is deleted — adding the directory again brings both back.`
        : "It leaves the rail. Adding the directory again brings it back.",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await updateProject(projectId, { archived: true });
      if (stagedProjectId() === projectId) setStagedProjectId(undefined);
      // The open thread goes with its directory. Archiving hides the section *and* the
      // threads under it, so leaving one mounted in the room would strand it: a
      // transcript on screen with no row anywhere in the rail, and no way back to it
      // once the operator navigates away.
      const open = currentId();
      if (open && rows()?.find((s) => s.id === open)?.projectId === projectId)
        setCurrentId(null);
    } catch (err) {
      toast.error(
        (err as { detail?: string })?.detail ??
          "Couldn't archive that directory",
      );
    }
  };

  /** Deleting keeps the threads and unfiles them — the backend's own rule, and the
   *  difference from archiving that decides which one the operator wants. */
  const remove = async (projectId: string, name: string) => {
    const ok = await confirm({
      title: `Delete ${name}?`,
      detail:
        "The directory on disk is untouched. Its threads are kept, with no project.",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteProject(projectId);
      if (stagedProjectId() === projectId) setStagedProjectId(undefined);
      refreshSessions();
    } catch (err) {
      toast.error(
        (err as { detail?: string })?.detail ??
          "Couldn't delete that directory",
      );
    }
  };

  // The rail's search, in the command strip above the list. `/` focuses it from
  // anywhere a bare key is not typing.
  const [query, setQuery] = createSignal("");
  let searchField: HTMLInputElement | undefined;
  registerKeymap(() => [{ combo: "/", run: () => searchField?.focus() }]);

  const count = () => rows()?.length ?? 0;

  // The unread set, from the backend's own notification read state: a thread with an
  // unread notification has something the operator has not seen.
  const notifications = useNotifications();
  const unreadIds = createMemo(
    () =>
      new Set(
        notifications.items
          .filter((n) => !n.readAt && n.conversationId)
          .map((n) => n.conversationId!),
      ),
  );

  return (
    <div class="flex min-h-0 flex-1 flex-col">
      {/* The command strip: the composer's own receded strip turned upside down —
          dim mono cells, no dividers, brightening only under the pointer or focus. */}
      <StatusBar
        variant="recede"
        edge="block"
        start={
          <StatusField
            ref={(el) => {
              searchField = el;
            }}
            leading="search"
            placeholder="Search"
            aria-label="Search threads"
            kbd="/"
            value={query()}
            onInput={setQuery}
            onEscape={() => {
              setQuery("");
              searchField?.blur();
            }}
          />
        }
        end={
          <>
            <Tooltip label="New thread (⌘⇧O)" side="bottom" delay={600}>
              <StatusCell onClick={() => startNew()} class="px-3">
                <Icon name="plus" size={16} />
                new
              </StatusCell>
            </Tooltip>
            <Tooltip label="Add directory" side="bottom" delay={600}>
              <StatusCell
                aria-label="Add directory"
                disabled={addDirectory.busy()}
                onClick={() => void addDirectory.add()}
                class="px-3"
              >
                <Icon name="library" size={16} />
              </StatusCell>
            </Tooltip>
          </>
        }
      />
      <ListGroupHeader
        variant="section"
        label="Recents"
        count={count()}
        aside={props.filter}
      />
      {/* The list takes whatever height is left rather than a fixed cap. The cap
          was right when six area sections sat beneath it and a long history would
          have pushed them off-screen; the rail is the thread list now, so a cap
          would leave dead space under it instead.

          No rule above the list (§7): a hairline here sat directly on top of the
          first row and read as a border belonging to *that item* rather than as
          a divider under the header. The header's own spacing separates them. */}
      <div class="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
        <SessionList
          sessions={rows}
          // Which row is *marked* open — `openId`, so a thread created by the turn
          // still running is highlighted the moment it is listed, rather than sitting
          // unselected under a transcript that is plainly its own.
          currentId={openId()}
          menu={threadMenu}
          onSelect={select}
          directories={directories()}
          status={status}
          actions={actions}
          onNewThread={startNew}
          // `openId` for the same reason the selection above uses it: a thread the
          // running turn has already created is not staged anywhere, and marking its
          // directory as staging while highlighting its row is the rail naming two
          // places for the next turn to land.
          stagedProjectId={
            openId() === null ? (stagedProjectId() ?? null) : null
          }
          query={query}
          unread={(id) => unreadIds().has(id)}
        />
      </div>
      {/* Both render nothing where they sit — the panel is portalled and the modal
          opens over the app — so they live at the end of the rail rather than inside
          the scroll body they act on. */}
      <ContextMenu api={threadMenu} items={threadActions} />
      {rename.element}
    </div>
  );
}
