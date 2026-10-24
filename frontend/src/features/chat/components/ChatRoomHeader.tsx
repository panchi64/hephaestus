import { Show, type JSX } from "solid-js";
import {
  Button,
  Frames,
  Icon,
  Menu,
  Text,
  TypewriterText,
  type MenuItem,
} from "~/ui";
import { REVEAL_SPEED_MS, pendingCount } from "../data";
import { TURN_LOCK_HINT } from "../turnLock";
import type { ChatViewport } from "../useChatViewport";
import type { BranchState } from "../data";
import { BranchChip } from "./BranchChip";
import { ViewportSurfaceBar } from "./ViewportSurfaceBar";

/** The session menu's entries — everything that acts on the thread rather than on a
 *  turn in it. Handed in as one object because they arrive as one: they are the
 *  session-actions menu, and splitting them into separate props only spread the same
 *  wiring across as many lines. */
export interface ChatRoomHeaderActions {
  rename: () => void;
  retitle: () => void;
  compact: () => void;
  openBrowser: () => void;
  copy: () => void;
  remove: () => void;
}

export interface ChatRoomHeaderProps {
  title: () => string;
  /** A title the backend has just written, for the typewriter reveal. */
  reveal: () => string | undefined;
  /** The directory a **staged** worktree thread will work in, `parent/name`. Absent for
   *  every saved thread, which names its workspace with the branch chip on the other end
   *  of this row instead — two answers to one question in one row is the thing to avoid.
   *  A staged thread has no branch yet and no row in the rail, so without this the
   *  operator has just clicked `+` on a directory and been shown nothing that names it. */
  workspaceHint: () => string | undefined;
  /** True while the thread is being named — the auto-title or a manual retitle. */
  working: () => boolean;
  conversationId: () => string | null;
  /** Length of the transcript, which is what makes compact and copy available. */
  messageCount: () => number;
  /** True while a hand-started fold is running on this thread — the menu row's own
   *  throbber. The room says so out loud in the composer's header line. */
  compacting: () => boolean;
  /** True while a turn is running, which switches off compacting and deleting. */
  turnLive: () => boolean;
  viewport: ChatViewport;
  /** The thread's branch, fetched by the room and shared with the Diff surface. */
  branch: () => BranchState | null | undefined;
  actions: ChatRoomHeaderActions;
}

/**
 * The thread's identity: what it is called — and nothing else on that side.
 *
 * An eyebrow above the name used to carry the model, the thread's age and its run state.
 * Each found a better home: the model is picked in the composer's status bar, where it is
 * a control rather than a second read-only copy of one, and the run state rides the
 * composer's header line beside the run clock, where the operator is looking while it
 * matters. The thread's age answered a question nobody was asking. What is left is a
 * title that floats over the transcript scrolling beneath it.
 *
 * **The other end of the row is what the thread can show you**, and that is a different
 * kind of thing from a name: the branch chip reads out what this thread has changed and
 * opens the patch, and beside it there is a button per surface the panel has something
 * to put in. Identity on the left, ways in on the right — so the row reads as one
 * question answered and one set of doors, rather than six controls in a line.
 *
 * **It carries no fill.** The room lays it over the top of the transcript, which
 * fades out beneath it through its own mask — so the header is a line of type over the
 * conversation rather than a band taking its own height above it.
 */
export function ChatRoomHeader(props: ChatRoomHeaderProps): JSX.Element {
  return (
    // `items-center` against the title block as a whole: with the workspace hint under
    // the name that block is two lines tall, and top-aligning would hang the session
    // controls level with the title alone, leaving a gap beneath them.
    <header class="flex items-center justify-between gap-3 pb-1">
      <span class="flex min-w-0 flex-col">
        <span class="flex min-w-0 items-center gap-2">
          <Show
            when={props.reveal()}
            fallback={
              // The interface's body step, bright and medium-weight: a thread's name is
              // a title over the room, not a hero figure (`readout` is the instrument
              // value style, design §4).
              <Text variant="body" tone="bright" class="truncate font-medium">
                {props.title()}
              </Text>
            }
          >
            {(title) => (
              <TypewriterText
                variant="body"
                tone="bright"
                text={title()}
                speed={REVEAL_SPEED_MS}
                // Same clamp as the static branch above. Without it a long
                // auto-generated name overflows the row while it types itself out
                // and then snaps to an ellipsis the moment the reveal hands over.
                class="truncate font-medium"
              />
            )}
          </Show>
          <Show when={props.working()}>
            <Frames class="shrink-0 text-info" />
          </Show>
        </span>
        {/* Where this thread will work, under the name it does not have yet. Quiet —
            it is context for the title, not a second title. */}
        <Show when={props.workspaceHint()}>
          {(hint) => (
            <span class="flex min-w-0 items-center gap-2">
              <Icon name="library" size={12} class="shrink-0 text-dim" />
              <Text variant="micro" tone="dim" class="min-w-0 truncate">
                {hint()}
              </Text>
            </span>
          )}
        </Show>
      </span>
      <div class="flex shrink-0 items-center gap-2">
        {/* A worktree thread's branch and diffstat, and the way into the patch.
            Renders nothing for a thread with no worktree — the backend answers 404
            for one, which is the ordinary case. */}
        <BranchChip
          branch={props.branch}
          onOpen={() => props.viewport.toggleSurface("diff")}
        />
        {/* Files the thread changed in the operator's own project folders and has not
            been told to put back — already in their working copy, which is why it is
            worth a control of its own. */}
        <Show when={pendingCount(props.viewport.changes())}>
          {(count) => (
            <Button
              variant="ghost"
              leading="edit"
              onClick={() => props.viewport.toggleSurface("diff")}
              aria-label={`Review ${count()} file${count() === 1 ? "" : "s"} this conversation changed in your projects`}
            >
              {count()} CHANGED
            </Button>
          )}
        </Show>
        {/* One button per surface that has anything to show — the eye toggle
            grown up. It could only ever say "the panel", which was enough while
            the panel held one thing and a guess once it holds several. */}
        <ViewportSurfaceBar viewport={props.viewport} />
        <Menu
          trigger={
            <Button variant="ghost" aria-label="Session actions">
              ···
            </Button>
          }
          items={
            [
              {
                label: "Rename conversation",
                icon: "edit",
                disabled: !props.conversationId(),
                onSelect: props.actions.rename,
              },
              {
                label: "Regenerate title",
                icon: "refresh",
                disabled: !props.conversationId(),
                onSelect: props.actions.retitle,
              },
              {
                label: "Compact now",
                icon: "layers",
                // The row's own throbber, rather than a swapped label: the menu has
                // closed by the time a fold is under way, so this is what the operator
                // sees if they open it again to check.
                pending: props.compacting(),
                // Whether a fold would take anything is the *backend's* arithmetic:
                // it retains the last N turns word for word, and N is an operator
                // setting this row cannot see. This used to guess at it with a
                // transcript length of 3, which matched no threshold the backend has
                // — it greyed the row out on threads that would have folded fine and
                // left it lit on threads that could only ever be refused. So the row
                // asks about the one thing the room does know, and the backend says
                // in its own words when it declines.
                disabled:
                  !props.conversationId() ||
                  props.messageCount() === 0 ||
                  props.turnLive(),
                hint: props.turnLive()
                  ? TURN_LOCK_HINT
                  : "There are no turns in this thread to fold yet.",
                onSelect: props.actions.compact,
              },
              {
                label: props.viewport.browser.active()
                  ? "Show browser"
                  : "Open browser",
                icon: "link",
                // The browser session belongs to the thread, so there is nothing to
                // open until there is a thread to open it for.
                disabled: !props.conversationId(),
                onSelect: props.actions.openBrowser,
              },
              {
                label: "Copy conversation",
                icon: "copy",
                disabled: props.messageCount() === 0,
                onSelect: props.actions.copy,
              },
              {
                label: "Delete conversation",
                icon: "trash",
                danger: true,
                disabled: !props.conversationId() || props.turnLive(),
                hint: TURN_LOCK_HINT,
                onSelect: props.actions.remove,
              },
            ] satisfies MenuItem[]
          }
        />
      </div>
    </header>
  );
}
