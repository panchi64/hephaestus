import {
  createEffect,
  createSignal,
  For,
  onCleanup,
  Show,
  Suspense,
  type JSX,
} from "solid-js";
import {
  Button,
  EmptyState,
  InstrumentBand,
  Input,
  ListRow,
  LoadingText,
  Menu,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Tooltip,
  confirm,
  toast,
  type MenuItem,
  type Status,
} from "~/ui";
import { isApiError } from "~/lib/api";
import { relativeTime, timestamp } from "~/lib/format";
import { createInFlight } from "~/lib/inFlight";
import {
  useRagSources,
  useIndexStats,
  refreshCorpus,
  addRagSource,
  removeRagSource,
  reindexSource,
  rebuildAllFolders,
} from "../data";
import type { RagIndexStatus, RagSource } from "../model";

const indexStatusFlag: Record<RagIndexStatus, Status> = {
  indexed: "nominal",
  indexing: "info",
  stale: "warn",
  error: "alert",
};

/** Surface a failed backend action without leaking raw errors. */
function reportError(fallback: string, err: unknown): void {
  toast.error(isApiError(err) ? err.detail : fallback);
}

export function RagConfigScreen(): JSX.Element {
  const sources = useRagSources();
  const stats = useIndexStats();
  const [newPath, setNewPath] = createSignal("");
  const [rebuilding, setRebuilding] = createSignal(false);

  // Reindex is the slowest action on this screen and the one that looks the most like
  // nothing happened. The endpoint is documented as 202 but the route awaits the whole
  // re-crawl and re-embed, so the call sits open for seconds to minutes — per source,
  // since the operator can and does start one folder and then another.
  const reindexing = createInFlight();
  // Keyed by the path, so the second Enter on an unchanged field is refused while the
  // first POST is still open; one field, so the control reads `any()`.
  const addingSource = createInFlight();

  const all = (): RagSource[] => sources() ?? [];
  const surfaces = () => all().filter((s) => s.kind === "surface");
  const folders = () => all().filter((s) => s.kind === "folder");
  const errorSources = () => all().filter((s) => s.status === "error");
  const degradedSources = () =>
    all().filter((s) => s.status === "stale" || s.status === "error");
  const anyIndexing = () => all().some((s) => s.status === "indexing");

  // Indexing runs server-side off the request path; poll the source list while any
  // source is mid-index so its status flips here when the backend finishes. The timer
  // lives in this consuming layer, not the data seam.
  createEffect(() => {
    if (!anyIndexing()) return;
    const timer = setInterval(refreshCorpus, 1500);
    onCleanup(() => clearInterval(timer));
  });

  async function startRebuild() {
    if (rebuilding()) return;
    setRebuilding(true);
    try {
      await rebuildAllFolders(all());
      toast.success("Rebuild started");
    } catch (err) {
      reportError("Rebuild failed", err);
    } finally {
      setRebuilding(false);
    }
  }

  async function handleAddSource() {
    const path = newPath().trim();
    if (!path) return;
    await addingSource.run(path, async () => {
      try {
        await addRagSource(path);
        setNewPath("");
        toast.success("Source added — indexing started", { duration: 5000 });
      } catch (err) {
        reportError("Could not add source", err);
      }
    });
  }

  async function handleRemove(id: string, label: string, docCount: number) {
    const ok = await confirm({
      title: `Remove source?`,
      detail: `"${label}" (${docCount} docs) will be removed from the knowledge base. Indexed data will be lost and retrieval for dependent chats may degrade.`,
      confirmLabel: "Remove",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await removeRagSource(id);
      toast.success("Source removed", {
        action: {
          label: "Undo",
          onClick: () => {
            void addRagSource(label)
              .then(() => toast.info("Source restored — reindexing"))
              .catch((err) => reportError("Could not restore source", err));
          },
        },
      });
    } catch (err) {
      reportError("Could not remove source", err);
    }
  }

  async function handleReindex(id: string, label: string) {
    await reindexing.run(id, async () => {
      // Announced before the call, not after it. The request only answers once the
      // re-crawl is finished, so the old order told the operator work was starting at
      // the exact moment it had stopped — and said nothing at all for the minutes in
      // between. Two toasts, one per edge, and the row throbs for the span between them.
      toast.info(`Reindexing ${label}…`);
      try {
        await reindexSource(id);
        toast.success(`${label} reindexed`);
      } catch (err) {
        reportError("Could not reindex", err);
      }
    });
  }

  /** One indexed-source row, shared by both the surfaces and folders panels.
   *  Surfaces link to the page that manages them (the whole row is a navigable
   *  link) and expose their single REINDEX action as a direct icon button;
   *  folders are static rows whose three actions live behind an overflow menu. */
  function sourceRow(source: RagSource): JSX.Element {
    const href =
      source.kind === "surface" ? (source.href ?? undefined) : undefined;
    const folderMenuItems: MenuItem[] = [
      {
        label: "Reindex",
        icon: "refresh",
        pending: reindexing.has(source.id),
        hint: "Already reindexing — this can take a few minutes.",
        onSelect: () => void handleReindex(source.id, source.label),
      },
      {
        label: "View docs",
        icon: "library",
        onSelect: () => toast.info("Document browser coming in Phase 2"),
      },
      {
        label: "Remove",
        icon: "trash",
        danger: true,
        onSelect: () =>
          void handleRemove(source.id, source.label, source.docCount),
      },
    ];

    return (
      <ListRow
        label={source.label}
        leading={source.icon}
        href={href}
        right={
          <span class="flex items-center gap-3 shrink-0">
            <Text variant="micro" tone="dim">
              {source.docCount} DOCS
            </Text>
            <Text variant="micro" tone="dim">
              {source.lastIndexedAt ? relativeTime(source.lastIndexedAt) : "—"}
            </Text>
            <StatusFlag status={indexStatusFlag[source.status]}>
              {source.status === "indexing"
                ? "Indexing…"
                : source.status.toUpperCase()}
            </StatusFlag>
            <Show
              when={source.kind === "folder"}
              fallback={
                <Tooltip label="Reindex this surface" side="top">
                  <Button
                    variant="ghost"
                    size="sm"
                    leading="refresh"
                    aria-label="Reindex this surface"
                    pending={reindexing.has(source.id)}
                    onClick={() => void handleReindex(source.id, source.label)}
                  />
                </Tooltip>
              }
            >
              <Menu
                trigger={
                  <span class="px-1 text-dim hover:text-bright">
                    <Text variant="micro">···</Text>
                  </span>
                }
                items={folderMenuItems}
              />
            </Show>
          </span>
        }
      />
    );
  }

  return (
    <Stack gap={6}>
      <PageHeader
        title="Knowledge base"
        subtitle="The unified retrieval corpus — every source the assistant can search."
        assetId="HEPH-RAG-01.0"
        actions={
          <Row gap={2}>
            <Show when={errorSources().length > 0}>
              <StatusFlag status="alert">{`${errorSources().length} ERROR`}</StatusFlag>
            </Show>
            <Button
              variant="primary"
              leading="refresh"
              onClick={() => void startRebuild()}
              pending={rebuilding()}
            >
              {rebuilding() ? "Rebuilding…" : "Rebuild index"}
            </Button>
          </Row>
        }
      />

      <Suspense fallback={<LoadingText />}>
        <InstrumentBand
          items={[
            {
              label: "Total docs",
              value:
                stats()?.totalDocs != null ? String(stats()!.totalDocs) : "—",
            },
            {
              label: "Collections",
              value: String(stats()?.totalCollections ?? "—"),
            },
            { label: "Embedding model", value: stats()?.embeddingModel ?? "—" },
            { label: "Dims", value: String(stats()?.dims ?? "—") },
            { label: "Store size", value: stats()?.storeSize ?? "—" },
          ]}
        />
      </Suspense>

      <Suspense fallback={<LoadingText />}>
        <Panel label="Corpus surfaces" flush>
          <Show
            when={surfaces().length > 0}
            fallback={
              <EmptyState
                icon="library"
                message="No surfaces"
                hint="Uploads, memory and conversations index here automatically."
              />
            }
          >
            <For each={surfaces()}>{(source) => sourceRow(source)}</For>
          </Show>
        </Panel>

        <Panel label="Indexed folders" flush>
          <Show
            when={folders().length > 0}
            fallback={
              <EmptyState
                icon="archive"
                message="No folders"
                hint="Add a host folder path below to start indexing."
              />
            }
          >
            <For each={folders()}>{(source) => sourceRow(source)}</For>
          </Show>
        </Panel>
      </Suspense>

      {/* Add folder source */}
      <Panel label="Add folder">
        <Stack gap={3}>
          <Row gap={3} align="end">
            <div class="flex-1">
              <Input
                label="Folder path"
                placeholder="/home/user/documents"
                value={newPath()}
                onInput={(e) => setNewPath(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newPath().trim())
                    void handleAddSource();
                }}
                type="text"
              />
            </div>
            <Button
              variant="primary"
              leading="plus"
              disabled={!newPath().trim()}
              pending={addingSource.any()}
              onClick={() => void handleAddSource()}
            >
              Add
            </Button>
          </Row>
          <Text variant="micro" tone="dim">
            Paths must be accessible to the Hephaestus server process. The
            folder will be crawled and all supported file types indexed.
          </Text>
        </Stack>
      </Panel>

      {/* Index health */}
      <Show when={degradedSources().length > 0}>
        <Panel label="Index health" state="alert">
          <Stack gap={2}>
            <Text variant="body" tone="warn">
              One or more sources are stale or unreachable. Retrieval quality
              may be degraded for affected collections.
            </Text>
            <For each={degradedSources()}>
              {(source) => (
                <Row gap={2} align="center">
                  <StatusFlag status={indexStatusFlag[source.status]}>
                    {source.status.toUpperCase()}
                  </StatusFlag>
                  <Text variant="body" class="font-mono">
                    {source.label}
                  </Text>
                  <Text variant="micro" tone="dim">
                    last:{" "}
                    {source.lastIndexedAt
                      ? timestamp(source.lastIndexedAt)
                      : "—"}
                  </Text>
                  <Show when={source.errorHint}>
                    <Text variant="micro" tone="alert">
                      {source.errorHint}
                    </Text>
                  </Show>
                  <Show when={source.status === "error"}>
                    <Tooltip label="Re-run indexing for this source" side="top">
                      <Button
                        variant="ghost"
                        leading="refresh"
                        pending={reindexing.has(source.id)}
                        onClick={() =>
                          void handleReindex(source.id, source.label)
                        }
                      >
                        Retry
                      </Button>
                    </Tooltip>
                    <Show when={source.kind === "folder"}>
                      <Button
                        variant="danger"
                        leading="trash"
                        onClick={() =>
                          void handleRemove(
                            source.id,
                            source.label,
                            source.docCount,
                          )
                        }
                      >
                        Remove
                      </Button>
                    </Show>
                  </Show>
                </Row>
              )}
            </For>
          </Stack>
        </Panel>
      </Show>
    </Stack>
  );
}
