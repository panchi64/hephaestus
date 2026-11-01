import { createMemo, For, Show, type JSX, type Resource } from "solid-js";
import {
  ListRow,
  Resource as ResourceView,
  Text,
  cx,
  type TextTone,
} from "~/ui";
import type { FileChange } from "../data";
import type { WorktreeListing } from "../data/worktreeFiles";
import { settled } from "~/lib/resource";
import { groupByDirectory, type FileTreeRow } from "./fileTreeRows";

/** What the thread's branch did to a path, for the one marker a row carries.
 *
 *  **The backend's word, shortened — never the frontend's judgement.** `status` is
 *  computed in `services/projects` and sent down finished; this maps it to a letter and
 *  nothing else. It does not read a path, and it does not decide a band. */
const MARKS: Record<FileChange["status"], string> = {
  added: "A",
  modified: "M",
  deleted: "D",
  renamed: "R",
  copied: "C",
  "type-changed": "T",
  unmerged: "U",
};

/** Monochrome, like the snapshot tree beside it: brightness separates changed from
 *  unchanged, and the two semantic accents stay reserved for the diff. */
function markTone(risk: FileChange["risk"] | undefined): TextTone {
  return risk === undefined ? "dim" : "bright";
}

/**
 * The thread's worktree, as it stands right now.
 *
 * **Not the snapshot tree with a different fetch.** That one lists a captured version
 * and every row it has carries a status, because a snapshot exists *because* something
 * changed. This lists a live checkout, where the overwhelming majority of files were
 * never touched by the thread at all and the changed ones are the minority worth
 * marking. The two also disagree about what "no files" means: an empty snapshot is a
 * snapshot of nothing, an empty worktree usually means there is no worktree yet.
 *
 * **The markers come from the branch, so they are suppressed unless the branch is what
 * answered.** When `root` is `"project"` the listing fell back to the operator's own
 * checkout, and painting this thread's changes over it would be showing one tree's edits
 * on another tree's files — the panel says which tree it is reading instead.
 */
export function WorktreeFileTree(props: {
  listing: Resource<WorktreeListing>;
  onRetry?: () => void;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
  /** The thread's changed files, keyed by path — empty when there is no branch. */
  changes: Map<string, FileChange>;
  /** Hide everything the thread did not touch. */
  editedOnly?: boolean;
  /** Lay out as a fixed side column with a rule, rather than filling. */
  inline?: boolean;
}): JSX.Element {
  // `settled`, never `props.listing()`. The listing's key carries the branch's
  // `lastCommitAt`/`filesChanged`, so the agent re-keys it on every commit it makes —
  // and a bare resource read inside a tracked scope registers with the nearest Suspense,
  // which is somewhere above this pane. The tree the operator is reading would drop
  // behind that fallback each time the thread touched a file. `settled` keeps the last
  // listing on screen while the next one is fetched, and `ResourceView` below still owns
  // the first-load and error arms in its own box.
  const current = (): WorktreeListing | undefined => settled(props.listing);

  // Derived once and read three times — the rows, the emptiness check the resource view
  // makes, and the trailing group. Recomputing them per read turned the containment test
  // below into a scan of the whole listing per changed file.
  const present = createMemo(() => new Set(current()?.paths ?? []));

  /** Paths the diff names but the listing does not contain: a deletion has no file left
   *  to list, and a rename's old path is gone by definition. Dropping them would drop
   *  the rows the merge gate ranks highest — a deletion is never `normal`. */
  const missing = createMemo(() =>
    current()?.root === "worktree"
      ? [...props.changes.keys()].filter((p) => !present().has(p))
      : [],
  );

  const rows = createMemo((): FileTreeRow[] => {
    const paths = current()?.paths ?? [];
    const shown = props.editedOnly
      ? paths.filter((p) => props.changes.has(p))
      : paths;
    return groupByDirectory(shown);
  });

  const fileRow = (
    row: Extract<FileTreeRow, { kind: "file" }>,
  ): JSX.Element => {
    const change = props.changes.get(row.path);
    return (
      <ListRow
        label={row.label}
        leading="file"
        selected={props.selectedPath === row.path}
        onClick={() => props.onSelectPath(row.path)}
        class={row.depth > 0 ? "pl-6" : undefined}
        right={
          <Text variant="micro" tone={markTone(change?.risk)}>
            {change ? MARKS[change.status] : "·"}
          </Text>
        }
      />
    );
  };

  return (
    <div
      class={cx(
        "flex min-h-0 flex-col",
        props.inline
          ? "w-56 shrink-0 border-r border-line @4xl:w-72"
          : "h-full flex-1",
      )}
    >
      <div class="min-h-0 flex-1 overflow-y-auto">
        <ResourceView
          data={props.listing}
          onRetry={props.onRetry}
          loadingLabel="Reading the workspace"
          isEmpty={() => rows().length === 0}
          emptyMessage={
            props.editedOnly ? "No files changed" : "No files in the workspace"
          }
        >
          {(listing) => (
            <>
              <Show when={listing().root === "project"}>
                {/* Said before the rows rather than after them: the operator is about to
                    read a tree, and which tree it is changes what the absence of a
                    marker means. */}
                <div class="px-3 py-2">
                  <Text variant="caption" tone="dim">
                    Showing the project checkout — this thread has no worktree
                    yet.
                  </Text>
                </div>
              </Show>
              <For each={rows()}>
                {(row) =>
                  row.kind === "dir" ? (
                    <div class="px-3 pb-1 pt-3">
                      <Text variant="micro" tone="dim" class="truncate">
                        {row.name}
                      </Text>
                    </div>
                  ) : (
                    fileRow(row)
                  )
                }
              </For>
              <Show when={missing().length > 0}>
                <div class="px-3 pb-1 pt-3">
                  <Text variant="caption" tone="dim">
                    Changed, not in the tree
                  </Text>
                </div>
                <For each={missing()}>
                  {(path) => {
                    const change = props.changes.get(path);
                    return (
                      <ListRow
                        label={path}
                        leading="file"
                        selected={props.selectedPath === path}
                        onClick={() => props.onSelectPath(path)}
                        right={
                          <Text variant="micro" tone={markTone(change?.risk)}>
                            {change ? MARKS[change.status] : "·"}
                          </Text>
                        }
                      />
                    );
                  }}
                </For>
              </Show>
              <Show when={listing().truncated}>
                {/* The cap is the backend's and it is stated rather than implied — a
                    partial tree that looks complete is the failure this prevents. */}
                <div class="px-3 py-2">
                  <Text variant="caption" tone="dim">
                    The workspace has more files than this list shows.
                  </Text>
                </div>
              </Show>
            </>
          )}
        </ResourceView>
      </div>
    </div>
  );
}
