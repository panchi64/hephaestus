import { createResource, createSignal, For, Show, type JSX } from "solid-js";
import {
  Button,
  DiffView,
  Panel,
  Resource,
  Row,
  Stack,
  StatusFlag,
  Text,
  confirm,
  toast,
  type Status,
} from "~/ui";
import { isApiError } from "~/lib/api";
import { settled } from "~/lib/resource";
import { bytes, longTimestamp, plural, relativeTime } from "~/lib/format";
import {
  getRevisionDiff,
  getUnpublishedChanges,
  listRevisions,
  patchStats,
  refreshRevisions,
  restoreRevision,
  revisionsTick,
  type ChangeStatus,
  type FileChange,
} from "./data";

const statusFlag: Record<ChangeStatus, Status> = {
  added: "nominal",
  removed: "alert",
  changed: "warn",
};

function ChangeList(props: { changes: FileChange[] }): JSX.Element {
  return (
    <Stack gap={4}>
      <For each={props.changes}>
        {(c) => {
          const stats = () => (c.patch ? patchStats(c.patch) : null);
          return (
            <Stack gap={2}>
              <Row gap={2} class="items-center">
                <StatusFlag status={statusFlag[c.status]}>
                  {c.status.toUpperCase()}
                </StatusFlag>
                <Text variant="code" tone="bright" class="min-w-0 break-all">
                  {c.path}
                </Text>
                <Show when={stats()}>
                  {(s) => (
                    <Text variant="micro" tone="dim">
                      +{s().added} −{s().removed}
                    </Text>
                  )}
                </Show>
              </Row>
              <Show
                when={c.patch}
                fallback={
                  <Text variant="meta" tone="dim">
                    Binary file — no text diff.
                  </Text>
                }
              >
                {(patch) => <DiffView diff={patch()} layout="hunks" />}
              </Show>
            </Stack>
          );
        }}
      </For>
    </Stack>
  );
}

/** Published versions of a skill or agent, the diff of each against the item as
 *  it is now, and the unpublished changes awaiting review. `base` is the item's
 *  revisions path (see `revisionsBase`). */
export function RevisionHistory(props: {
  base: string;
  /** Called after a restore, so the screen refetches the item. */
  onRestored: () => void;
}): JSX.Element {
  const [selected, setSelected] = createSignal<number | null>(null);
  const [restoring, setRestoring] = createSignal(false);

  const [revisions] = createResource(
    () => [props.base, revisionsTick()] as const,
    ([base]) => listRevisions(base),
  );
  const [pending] = createResource(
    () => [props.base, revisionsTick()] as const,
    ([base]) => getUnpublishedChanges(base),
  );
  const [diff] = createResource(
    () => {
      const n = selected();
      return n === null ? false : ([props.base, n, revisionsTick()] as const);
    },
    ([base, n]) => getRevisionDiff(base, n),
  );

  const pendingChanges = (): FileChange[] => settled(pending) ?? [];

  async function restore(n: number): Promise<void> {
    const ok = await confirm({
      title: `Restore version ${n}?`,
      detail:
        "The current contents are replaced with this version. A published item records the restore as a new version.",
      confirmLabel: "Restore",
    });
    if (!ok) return;
    setRestoring(true);
    try {
      await restoreRevision(props.base, n);
      toast.success(`Restored version ${n}`);
      setSelected(null);
      props.onRestored();
    } catch (err) {
      toast.error(
        isApiError(err) ? err.detail : `Could not restore version ${n}.`,
      );
    } finally {
      setRestoring(false);
    }
  }

  return (
    <Resource
      data={revisions}
      loadingLabel="Loading history"
      errorMessage="Could not load the history"
      onRetry={refreshRevisions}
      isEmpty={(rows) => rows.length === 0}
      emptyMessage="No versions yet"
      emptyHint="Versions are recorded each time this is published."
    >
      {(rows) => (
        <Stack gap={4}>
          <Show when={pendingChanges().length > 0}>
            <Panel
              label="Unpublished changes"
              meta={
                <Text variant="micro" tone="dim">
                  {plural(pendingChanges().length, "file")} since version{" "}
                  {rows()[0].number}
                </Text>
              }
            >
              <ChangeList changes={pending() ?? []} />
            </Panel>
          </Show>
          <Panel label="Versions" meta={`${rows().length}`} flush>
            <Stack>
              <For each={rows()}>
                {(r) => (
                  <div class="border-b border-line last:border-b-0">
                    <button
                      type="button"
                      class="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-hover"
                      aria-expanded={selected() === r.number}
                      onClick={() =>
                        setSelected(selected() === r.number ? null : r.number)
                      }
                    >
                      <Text variant="code" tone="bright">
                        v{r.number}
                      </Text>
                      <Text variant="body" class="min-w-0 flex-1">
                        {r.note}
                      </Text>
                      <Text variant="micro" tone="dim">
                        {bytes(r.sizeBytes)}
                      </Text>
                      <Text
                        variant="micro"
                        tone="dim"
                        title={longTimestamp(r.createdAt)}
                      >
                        {relativeTime(r.createdAt)}
                      </Text>
                    </button>
                    <Show when={selected() === r.number}>
                      <Stack gap={3} class="px-3 pb-3">
                        <Row gap={2} class="items-center justify-between">
                          <Text variant="meta" tone="dim">
                            {longTimestamp(r.createdAt)} · compared with now
                          </Text>
                          <Button
                            variant="default"
                            size="sm"
                            pending={restoring()}
                            onClick={() => void restore(r.number)}
                          >
                            Restore this version
                          </Button>
                        </Row>
                        <Resource
                          data={diff}
                          loadingLabel="Loading diff"
                          errorMessage="Could not load the diff"
                          isEmpty={(c) => c.length === 0}
                          emptyMessage="Identical to the current contents"
                        >
                          {(changes) => <ChangeList changes={changes()} />}
                        </Resource>
                      </Stack>
                    </Show>
                  </div>
                )}
              </For>
            </Stack>
          </Panel>
        </Stack>
      )}
    </Resource>
  );
}
