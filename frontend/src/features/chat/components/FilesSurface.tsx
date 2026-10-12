import {
  createEffect,
  createMemo,
  createResource,
  createSignal,
  on,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { api } from "~/lib/api";
import { Button, CodeBlock, EmptyState, LoadingText, Text } from "~/ui";
import type { BranchState, FileChange } from "../data";
import {
  fetchWorktreeFiles,
  fetchWorktreeFileText,
  worktreeFilePath,
} from "../data/worktreeFiles";
import { extensionOf } from "../viewport/viewItems";
import { createDownloadSlot } from "../viewport/downloadRegistry";
import { PaneToolbar } from "./PaneFrame";
import { WorktreeFileTree } from "./WorktreeFileTree";
import { settled } from "~/lib/resource";

/** Below this the tree and a file cannot share a row, so the pane shows one at a
 *  time. `w-56` of tree against a 320px pane leaves ~96px for the file.
 *
 *  Measured rather than a container class, because the width decides behaviour and
 *  not only layout: whether a file is auto-selected, and whether picking one replaces
 *  the tree. CSS can hide a box; it cannot tell the effect below which arm is on. */
const SIDE_BY_SIDE = 560;

/**
 * The workspace, browsable on its own.
 *
 * **It reads the worktree, not a snapshot of one.** It used to list the newest View
 * version's files, which meant the panel was empty — and unavailable — until the agent
 * happened to call `view_show`. A worktree thread that edits ten files and never captures a
 * version is an ordinary worktree thread, and it showed nothing. The tree now comes from the
 * same listing the composer's `@` picker reads, resolved by the backend against this
 * thread's own worktree.
 *
 * **It is not the View's CODE tab, and that tab is unchanged.** CODE compares two
 * *captured* versions and owns the pickers for choosing them; there is no live equivalent
 * of a comparison between two moments that were never recorded. This is the workspace as
 * a place you go, which is what it is.
 *
 * **What changed is the branch's word, joined here by path.** The marker on a row comes
 * from `BranchState.files`, which the backend already ranked and classified; this puts
 * two server answers beside each other and renders both verbatim. It classifies nothing.
 *
 * **Narrow, it is one thing at a time.** A tree beside a file is the right layout when
 * there is room for both and an unreadable pair when there is not, so below
 * `SIDE_BY_SIDE` picking a file replaces the list and a back control returns to it.
 */
export function FilesSurface(props: {
  /** The thread's branch — the source of both the ids to read by and the change marks. */
  branch: () => BranchState | null | undefined;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  // The listing re-reads when the branch tip moves. `diff` commits the worktree before
  // it reads, so `lastCommitAt` is the backend's own statement of when these files last
  // changed — a truer refresh trigger than a turn counter, and the reason this needs no
  // filesystem watcher (there is none in the backend, by design).
  const key = createMemo(() => {
    const b = props.branch();
    if (!b) return undefined;
    return [
      b.projectId,
      b.conversationId,
      b.lastCommitAt ?? "",
      b.filesChanged,
    ] as const;
  });

  const [listing, { refetch }] = createResource(
    key,
    ([projectId, conversationId]) =>
      fetchWorktreeFiles(projectId, conversationId),
  );

  const changes = createMemo(() => {
    const rows = props.branch()?.files ?? [];
    return new Map<string, FileChange>(rows.map((f) => [f.path, f]));
  });

  const [selectedPath, setSelectedPath] = createSignal<string | null>(null);
  const [editedOnly, setEditedOnly] = createSignal(false);
  const [width, setWidth] = createSignal(0);
  const sideBySide = (): boolean => width() >= SIDE_BY_SIDE;

  // Default to the first file the operator is likely to want: the first *changed* one
  // when the thread has changed anything, since that is what they came to look at.
  //
  // **Only where a file is on screen anyway.** Narrow, the pane shows the tree *or* a
  // file, and clearing the selection is how the operator gets back to the tree — so an
  // auto-select that fires whenever the selection is empty re-picks a file the instant
  // the back control clears one, and the control does nothing. Side by side there is a
  // viewer either way and a default is a courtesy; narrow, the list is the right
  // landing and the operator opens what they want from it.
  createEffect(() => {
    if (!sideBySide()) return;
    const rows = listing();
    if (!rows || rows.paths.length === 0) return;
    if (selectedPath() !== null) return;
    const edited = rows.paths.find((p) => changes().has(p));
    setSelectedPath(edited ?? rows.paths[0]);
  });

  // A thread change invalidates the pick; a re-read of the *same* thread does not —
  // reselecting on every turn would yank the file the operator is reading out from under
  // them. `defer` so this does not fire on mount and fight the default-select above.
  createEffect(
    on(
      () => props.branch()?.conversationId,
      () => setSelectedPath(null),
      { defer: true },
    ),
  );

  const fileKey = createMemo(() => {
    const b = props.branch();
    const path = selectedPath();
    return b && path
      ? ([b.projectId, b.conversationId, path, b.lastCommitAt ?? ""] as const)
      : undefined;
  });

  const [text] = createResource(fileKey, ([projectId, conversationId, path]) =>
    fetchWorktreeFileText(projectId, conversationId, path),
  );

  // Arm the panel's download with whatever file is on screen.
  const armDownload = createDownloadSlot();
  createEffect(() => {
    const b = props.branch();
    const path = selectedPath();
    if (!b || !path) {
      armDownload(null);
      return;
    }
    armDownload({
      name: path.split("/").pop() ?? path,
      getBlob: () =>
        api.getBlob(worktreeFilePath(b.projectId, b.conversationId, path)),
    });
  });

  const tree = (inline: boolean): JSX.Element => (
    <WorktreeFileTree
      listing={listing}
      onRetry={() => void refetch()}
      selectedPath={selectedPath()}
      onSelectPath={setSelectedPath}
      changes={changes()}
      editedOnly={editedOnly()}
      inline={inline}
    />
  );

  const viewer = (): JSX.Element => (
    <div class="flex min-h-0 min-w-0 flex-1 flex-col">
      <Show when={!sideBySide()}>
        <div class="flex shrink-0 items-center gap-2 px-3 py-1.5">
          <Button
            variant="ghost"
            size="sm"
            leading="chevron-left"
            onClick={() => setSelectedPath(null)}
          >
            Files
          </Button>
          <Text variant="micro" tone="dim" class="min-w-0 truncate">
            {selectedPath()}
          </Text>
        </div>
      </Show>
      <div class="min-h-0 flex-1 overflow-auto">
        {/* `latest`, not the resource: read mid-fetch inside a tracked scope it
            suspends the nearest `Suspense` — the pane's — so picking the next
            file would blank the pane rather than hold the current file until
            the new one lands. */}
        <Show when={settled(text) !== undefined} fallback={<LoadingText />}>
          <Show
            when={!settled(text)?.unreadable}
            fallback={
              // The listing and the read disagree by design — git lists a symlink
              // pointing out of the tree and the containment check refuses to open it.
              // Said in a sentence rather than left as a spinner.
              <div class="px-3 py-2">
                <Text variant="micro" tone="dim">
                  This file is in the listing but cannot be opened from here.
                </Text>
              </div>
            }
          >
            <CodeBlock
              code={settled(text)?.text ?? ""}
              lang={extensionOf(selectedPath()) ?? undefined}
              fontStep={props.fontStep}
              softWrap={props.softWrap}
            />
          </Show>
          <Show when={settled(text)?.truncated}>
            {/* The backend cut the file at its ceiling and said so in a header. A
                viewer showing the first part of a file has to repeat that, or it
                reads as the whole file. */}
            <div class="px-3 py-2">
              <Text variant="micro" tone="dim">
                This file is longer than the viewer will load.
              </Text>
            </div>
          </Show>
        </Show>
      </div>
    </div>
  );

  return (
    <div
      ref={(el) => {
        const observer = new ResizeObserver(() => setWidth(el.clientWidth));
        observer.observe(el);
        setWidth(el.clientWidth);
        // A surface is closed and reopened as often as the operator likes, and an
        // observer left running holds its detached element for the life of the tab.
        onCleanup(() => observer.disconnect());
      }}
      class="flex h-full min-h-0 flex-col"
    >
      <Show
        when={props.branch()}
        fallback={
          <EmptyState
            icon="file"
            message="No workspace yet"
            hint="A worktree thread's files appear here once it has a branch to work on."
          />
        }
      >
        {/* The name is the pane frame's; these are controls over this surface's
            own state rather than figures about it, lent to the frame's header
            instead of spending a row of their own. */}
        <PaneToolbar>
          <Button
            variant="ghost"
            size="sm"
            active={editedOnly()}
            aria-pressed={editedOnly()}
            onClick={() => setEditedOnly(!editedOnly())}
          >
            Edited only
          </Button>
          {/* The tree re-reads when the branch tip moves, which covers the agent's own
              edits. This is for the other writer: the operator, in their editor. */}
          <Button
            variant="ghost"
            size="sm"
            leading="refresh"
            onClick={() => void refetch()}
            aria-label="Re-read the workspace"
          />
        </PaneToolbar>
        <div class="flex min-h-0 flex-1">
          <Show
            when={sideBySide()}
            fallback={
              <Show when={selectedPath() !== null} fallback={tree(false)}>
                {viewer()}
              </Show>
            }
          >
            {tree(true)}
            {viewer()}
          </Show>
        </div>
      </Show>
    </div>
  );
}
