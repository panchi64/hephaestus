import { For, Show, type JSX, type Resource } from "solid-js";
import {
  ListRow,
  Resource as ResourceView,
  Text,
  cx,
  type TextTone,
} from "~/ui";
import type { SnapshotFile } from "../model";

/** The tone + single-letter marker for a file's change status. Kept monochrome —
 *  brightness separates changed (bright) from unchanged (dim), per the design
 *  system's color discipline; the two semantic accents are reserved for the diff. */
export function statusTone(status: SnapshotFile["status"]): TextTone {
  return status === "unchanged" ? "dim" : "bright";
}

export function statusMark(status: SnapshotFile["status"]): string {
  return status === "added" ? "A" : status === "modified" ? "M" : "·";
}

/** Whether a file changed in this snapshot. The backend already answers this per
 *  file, so "only what was edited" is a predicate over a list we have rather than a
 *  second request.
 *
 *  Local: the Files surface used to import it to pick a default file, and now reads a
 *  live worktree whose changed set comes from the branch instead. */
const isEdited = (f: SnapshotFile): boolean => f.status !== "unchanged";

/**
 * A snapshot's files, as a pickable list.
 *
 * Shared by the View's CODE tab and the Files surface, which show the same tree for
 * different reasons — one to read a version's source beside its preview, the other to
 * browse the workspace on its own. Two copies of a file list would be two places to fix
 * the day it grows folders.
 *
 * **It is not always a fixed column.** At `w-56` beside a content pane it leaves ~96px
 * for the file itself once the panel is at its 320px minimum, which is not a file
 * viewer. `inline` is a capped band above the file below the pane's `@xl` and a side
 * column from there; without it the list fills whatever it is given, which is what
 * lets a narrow pane show the tree *or* the file rather than an unreadable pair.
 */
export function SnapshotFileTree(props: {
  files: Resource<SnapshotFile[]>;
  onRetry?: () => void;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
  /** Hide files this snapshot did not touch. */
  editedOnly?: boolean;
  /** A line above the list — the snapshot's own summary, where there is one. */
  summary?: string;
  /** Lay out as a fixed side column with a rule, rather than filling. */
  inline?: boolean;
}): JSX.Element {
  const rowsOf = (rows: SnapshotFile[]): SnapshotFile[] =>
    props.editedOnly ? rows.filter(isEdited) : rows;

  return (
    <div
      class={cx(
        "flex min-h-0 flex-col",
        // Inline, it is a capped band over the file on a narrow pane and a side
        // column once the pane can spare one — the caller flips its own row to match.
        props.inline
          ? "max-h-48 shrink-0 border-b border-line @xl:max-h-none @xl:w-56 @xl:border-r @xl:border-b-0 @4xl:w-72"
          : "h-full flex-1",
      )}
    >
      <Show when={props.summary}>
        <div class="px-3 py-2">
          <Text variant="micro" tone="dim">
            {props.summary}
          </Text>
        </div>
      </Show>
      <div class="min-h-0 flex-1 overflow-y-auto">
        <ResourceView
          data={props.files}
          onRetry={props.onRetry}
          loadingLabel="Loading files"
          isEmpty={(rows) => rowsOf(rows).length === 0}
          emptyMessage={props.editedOnly ? "No files changed" : "No files"}
        >
          {(rows) => (
            <For each={rowsOf(rows())}>
              {(file) => (
                <ListRow
                  label={file.path}
                  leading="file"
                  selected={props.selectedPath === file.path}
                  onClick={() => props.onSelectPath(file.path)}
                  right={
                    <Text variant="micro" tone={statusTone(file.status)}>
                      {statusMark(file.status)}
                    </Text>
                  }
                />
              )}
            </For>
          )}
        </ResourceView>
      </div>
    </div>
  );
}
