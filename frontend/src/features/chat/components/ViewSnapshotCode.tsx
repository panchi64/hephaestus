import {
  createEffect,
  createMemo,
  createResource,
  createSignal,
  Match,
  Show,
  Switch,
  type JSX,
  type Resource,
} from "solid-js";
import { api } from "~/lib/api";
import {
  CodeBlock,
  DiffView,
  EmptyState,
  ErrorState,
  LoadingText,
  Select,
  Text,
  type SelectOption,
} from "~/ui";
import {
  fetchSnapshotDiffs,
  fetchSnapshotFiles,
  fetchSnapshotFileText,
  snapshotFilePath,
} from "../data";
import type { SnapshotFile, ViewSnapshotRef } from "../model";
import { extensionOf, type PriorVersion } from "../viewport/viewItems";
import { rememberScroll } from "../scrollMemory";
import { createDownloadSlot } from "../viewport/downloadRegistry";
import { SnapshotFileTree } from "./SnapshotFileTree";
import { settled } from "~/lib/resource";

/** "Compare vs" value for plain code (no diff). */
const NO_DIFF = "";

/**
 * Renders a workspace snapshot's CODE — a left file list (with change-status markers)
 * selecting a file, and a content pane showing either its full source or a unified
 * diff. FROM and TO are both freely selectable versions (TO defaults to the entry on
 * stage, FROM to the version immediately before it) — the backend's per-snapshot diff
 * endpoint takes any snapshot id as the owning "TO" and any prior id as `base`, so both
 * ends are real server data, not a client-side approximation. The file list and the
 * selected file are owned by the stage and passed in, so they survive a PREVIEW/CODE
 * flip. The frontend only displays what the snapshot endpoints return; it decides nothing.
 */
export function ViewSnapshotCode(props: {
  snapshot: ViewSnapshotRef;
  files: Resource<SnapshotFile[]>;
  /** Retries the owning stage's file-list fetch (armed on `files`'s own refetch);
   *  only meaningful when TO is the entry on stage — an older TO's list is fetched
   *  locally below and retries itself via `toFiles`'s own resource. */
  onRetryFiles?: () => void;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
  /** Prior snapshots, chronological (oldest → newest); the last is the previous. */
  priorVersions: PriorVersion[];
  /** Forwarded to `CodeBlock`/`DiffView` — the panel's zoom/wrap controls. */
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const id = (): string => props.snapshot.snapshotId;

  // Every version this entry can stand in for as TO: its priors, then itself
  // (the default, and the newest option).
  const allVersions = createMemo<PriorVersion[]>(() => [
    ...props.priorVersions,
    { id: id(), label: "This version" },
  ]);

  // TO — defaults to the entry on stage; freely selectable among any candidate.
  const [toId, setToId] = createSignal(id());
  // FROM candidates are every version strictly older than the selected TO.
  const fromCandidates = createMemo<PriorVersion[]>(() => {
    const all = allVersions();
    const i = all.findIndex((v) => v.id === toId());
    return i <= 0 ? [] : all.slice(0, i);
  });
  const defaultFromId = createMemo(
    () => fromCandidates().at(-1)?.id ?? NO_DIFF,
  );
  // Explicit FROM pick; null = follow the default (the version immediately
  // before TO). Reset whenever TO changes so a stale pick can't outlive it.
  const [fromPick, setFromPick] = createSignal<string | null>(null);
  const fromId = createMemo(() => fromPick() ?? defaultFromId());

  const setTo = (v: string): void => {
    setToId(v);
    setFromPick(null);
  };

  // Full source of TO — only when comparing against nothing.
  const [text, { refetch: refetchText }] = createResource(
    () => {
      const path = props.selectedPath;
      return fromId() === NO_DIFF && path
        ? ([toId(), path] as const)
        : undefined;
    },
    ([snapshotId, path]) => fetchSnapshotFileText(snapshotId, path),
  );

  // TO's own file list — reused from the stage when TO is the entry on stage
  // (the common case, avoiding a redundant fetch); fetched locally only when
  // the operator picks an older TO.
  const [toFiles, { refetch: refetchToFiles }] = createResource(
    () => (toId() !== id() ? toId() : undefined),
    fetchSnapshotFiles,
  );

  // Diffs against the chosen FROM, owned by TO — fetched lazily, only when a
  // base is selected.
  const [diffs, { refetch: refetchDiffs }] = createResource(
    () =>
      fromId() !== NO_DIFF
        ? ([toId(), fromId()] as [string, string])
        : undefined,
    ([snapshotId, b]) => fetchSnapshotDiffs(snapshotId, b),
  );
  // `latest` throughout this file, never the resource itself: a resource read
  // mid-fetch inside a tracked scope suspends the nearest `Suspense` — the
  // pane's — so picking a file or a compare base would drop the whole pane to a
  // Loading line instead of holding the file that is on screen until the next
  // one arrives. The arms below already say what to render with nothing in hand.
  const selectedDiff = createMemo(() => {
    const path = props.selectedPath;
    return path ? settled(diffs)?.find((d) => d.path === path) : undefined;
  });

  const toOptions = createMemo<SelectOption[]>(() =>
    allVersions().map((v) => ({ value: v.id, label: v.label })),
  );
  const fromOptions = createMemo<SelectOption[]>(() => [
    { value: NO_DIFF, label: "No diff · full code" },
    ...[...fromCandidates()].reverse().map((v) => ({
      value: v.id,
      label: `Diff vs ${v.label}`,
    })),
  ]);

  // The currently selected file downloads from the entry on stage (this
  // component's own snapshot — independent of whichever TO/FROM is active in
  // the compare selectors above).
  const armDownload = createDownloadSlot();
  createEffect(() => {
    const path = props.selectedPath;
    if (!path) {
      armDownload(null);
      return;
    }
    const snapshotId = id();
    armDownload({
      name: path.split("/").pop() ?? path,
      getBlob: () => api.getBlob(snapshotFilePath(snapshotId, path)),
    });
  });

  return (
    <div class="flex h-full min-h-0 flex-col @xl:flex-row">
      {/* File tree — pick a file; its change status reads through tone. Shared
          with the Files surface, which shows the same list for a different
          reason. Stacked above the file on a narrow pane, beside it from `@xl`. */}
      <SnapshotFileTree
        inline
        files={toId() === id() ? props.files : toFiles}
        onRetry={toId() === id() ? props.onRetryFiles : refetchToFiles}
        selectedPath={props.selectedPath}
        onSelectPath={props.onSelectPath}
        summary={props.snapshot.summary}
      />

      {/* Content — full code or a diff against the chosen FROM. */}
      <div class="flex min-h-0 min-w-0 flex-1 flex-col">
        <div class="flex items-center gap-2 px-3 py-2">
          <Text variant="micro" tone="dim" class="shrink-0">
            To
          </Text>
          <Select
            aria-label="Compare TO version"
            class="min-w-0 flex-1"
            options={toOptions()}
            value={toId()}
            onChange={setTo}
          />
          <Text variant="micro" tone="dim" class="shrink-0">
            From
          </Text>
          <Select
            aria-label="Compare FROM version"
            class="min-w-0 flex-1"
            options={fromOptions()}
            value={fromId()}
            onChange={(v) => setFromPick(v)}
          />
        </div>
        <div class="min-h-0 flex-1">
          <Show
            when={props.selectedPath}
            fallback={
              <EmptyState
                message="No file selected"
                hint="Pick a file to view."
              />
            }
            keyed
          >
            {(path) => {
              // `CodeBlock`/`DiffView` each own their scroll root, so scroll
              // persistence hooks onto whichever one is on stage rather than a
              // wrapper — a second `overflow-auto` around them nested one
              // scroller inside another and left the diff scrolling in the
              // wrong box.
              const scrollRef = (el: HTMLElement): void =>
                rememberScroll(
                  el,
                  () => `${id()}:${toId()}:${fromId()}:${path}`,
                );
              return (
                <div class="h-full min-h-0">
                  <Switch>
                    {/* Full code. */}
                    <Match when={fromId() === NO_DIFF}>
                      <Switch fallback={<LoadingText label="Loading file" />}>
                        <Match when={text.error}>
                          <ErrorState
                            message="Could not load this file."
                            onRetry={() => void refetchText()}
                          />
                        </Match>
                        <Match when={settled(text) !== undefined}>
                          <CodeBlock
                            ref={scrollRef}
                            code={settled(text)!}
                            lang={extensionOf(path) ?? undefined}
                            fontStep={props.fontStep}
                            softWrap={props.softWrap}
                          />
                        </Match>
                      </Switch>
                    </Match>

                    {/* Diff against the chosen FROM. */}
                    <Match when={fromId() !== NO_DIFF}>
                      <Switch fallback={<LoadingText label="Loading diff" />}>
                        <Match when={diffs.error}>
                          <ErrorState
                            message="Could not load this diff."
                            onRetry={() => void refetchDiffs()}
                          />
                        </Match>
                        <Match when={settled(diffs) && selectedDiff()?.diff}>
                          <DiffView
                            ref={scrollRef}
                            diff={selectedDiff()!.diff}
                            fontStep={props.fontStep}
                            softWrap={props.softWrap}
                          />
                        </Match>
                        <Match when={settled(diffs) && !selectedDiff()?.diff}>
                          <EmptyState
                            message="No diff"
                            hint="This file is unchanged between the selected versions (or its diff is empty)."
                          />
                        </Match>
                      </Switch>
                    </Match>
                  </Switch>
                </div>
              );
            }}
          </Show>
        </div>
      </div>
    </div>
  );
}
