import { createMemo, createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  Checkbox,
  Divider,
  EmptyState,
  InstrumentBand,
  ListRow,
  ListToolbar,
  LoadingText,
  Menu,
  Modal,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Textarea,
  Input,
  confirm,
  toast,
} from "~/ui";
import { createListView } from "~/lib/list";
import { num, relativeTime } from "~/lib/format";
import {
  addMemory,
  auditDuplicates,
  deleteMemory,
  recall,
  updateMemory,
  useMemories,
} from "../data";
import type { DuplicateGroup, Memory, RecallHit } from "../model";

export function MemoryTimelineScreen(): JSX.Element {
  const memories = useMemories();

  // Read `.latest`, never the resource accessor: the InstrumentBand and the
  // toolbar's count()/total() render outside the list's <Suspense>, so a
  // suspending read would bubble to the app's fallback-less root <Suspense> and
  // blank the whole page on initial load. `.latest` never suspends.
  const view = createListView<Memory>({
    source: () => settled(memories) ?? [],
    search: (m) => m.content,
    sorts: {
      recent: {
        label: "Newest",
        compare: (a, b) => a.createdAt.localeCompare(b.createdAt),
      },
    },
    initialSort: "recent",
    initialDir: "desc",
  });

  const counts = createMemo(() => {
    const all = settled(memories) ?? [];
    let pinned = 0;
    let embedded = 0;
    for (const m of all) {
      if (m.pinned) pinned++;
      if (m.hasEmbedding) embedded++;
    }
    return { total: all.length, pinned, embedded };
  });

  /* ── Add / edit ─────────────────────────────────────────────────────────── */
  const [editing, setEditing] = createSignal<Memory | null>(null);
  const [composeOpen, setComposeOpen] = createSignal(false);
  const [draft, setDraft] = createSignal("");
  const [draftPinned, setDraftPinned] = createSignal(false);
  const [saving, setSaving] = createSignal(false);

  const openAdd = () => {
    setEditing(null);
    setDraft("");
    setDraftPinned(false);
    setComposeOpen(true);
  };
  const openEdit = (m: Memory) => {
    setEditing(m);
    setDraft(m.content);
    setDraftPinned(m.pinned);
    setComposeOpen(true);
  };
  const saveDraft = async () => {
    const content = draft().trim();
    if (!content || saving()) return;
    setSaving(true);
    try {
      const target = editing();
      if (target) {
        await updateMemory(target.id, { content, pinned: draftPinned() });
        toast.success("Memory updated");
      } else {
        await addMemory(content, draftPinned());
        toast.success("Memory added");
      }
      setComposeOpen(false);
    } catch {
      toast.error("Unable to save the memory.");
    } finally {
      setSaving(false);
    }
  };

  const handlePin = async (m: Memory) => {
    try {
      await updateMemory(m.id, { pinned: !m.pinned });
      toast.success(m.pinned ? "Memory unpinned" : "Memory pinned");
    } catch {
      toast.error("Unable to update the memory.");
    }
  };

  const handleDelete = async (m: Memory) => {
    const preview =
      m.content.length > 80 ? m.content.slice(0, 80) + "…" : m.content;
    if (
      !(await confirm({
        title: "Delete memory?",
        detail: `"${preview}" — this cannot be undone.`,
        confirmLabel: "Delete",
        tone: "alert",
      }))
    )
      return;
    try {
      await deleteMemory(m.id);
      toast.success("Memory deleted");
    } catch {
      toast.error("Unable to delete the memory.");
    }
  };

  /* ── Recall ─────────────────────────────────────────────────────────────── */
  const [recallOpen, setRecallOpen] = createSignal(false);
  const [recallQuery, setRecallQuery] = createSignal("");
  const [recallHits, setRecallHits] = createSignal<RecallHit[] | null>(null);
  const [recalling, setRecalling] = createSignal(false);
  const runRecall = async () => {
    const q = recallQuery().trim();
    if (!q || recalling()) return;
    setRecalling(true);
    try {
      setRecallHits(await recall(q, 8));
    } catch {
      toast.error("Recall failed.");
    } finally {
      setRecalling(false);
    }
  };

  /* ── Dedup audit ────────────────────────────────────────────────────────── */
  const [dedupOpen, setDedupOpen] = createSignal(false);
  const [groups, setGroups] = createSignal<DuplicateGroup[] | null>(null);
  const [auditing, setAuditing] = createSignal(false);
  const openDedup = async () => {
    setDedupOpen(true);
    setAuditing(true);
    try {
      setGroups(await auditDuplicates(settled(memories) ?? []));
    } catch {
      toast.error("Audit failed.");
      setGroups([]);
    } finally {
      setAuditing(false);
    }
  };
  const deleteFromGroup = async (m: Memory) => {
    try {
      await deleteMemory(m.id);
      setGroups((gs) =>
        (gs ?? [])
          .map((g) => ({
            ...g,
            memories: g.memories.filter((x) => x.id !== m.id),
          }))
          .filter((g) => g.memories.length > 1),
      );
      toast.success("Memory deleted");
    } catch {
      toast.error("Unable to delete the memory.");
    }
  };

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Memory"
        subtitle="Persistent facts, preferences, and context the system has learned."
        assetId="ODY-MEM-01.0"
        actions={
          <Row gap={2}>
            <Button
              variant="default"
              leading="search"
              onClick={() => {
                setRecallHits(null);
                setRecallQuery("");
                setRecallOpen(true);
              }}
            >
              Recall
            </Button>
            <Button variant="default" leading="copy" onClick={openDedup}>
              Dedup audit
            </Button>
            <Button variant="primary" leading="plus" onClick={openAdd}>
              Add memory
            </Button>
          </Row>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(counts().total) },
          { label: "Pinned", value: String(counts().pinned), tone: "nominal" },
          {
            label: "Embedded",
            value: String(counts().embedded),
            tone: "info",
          },
        ]}
      />

      <Panel flush>
        <div class="p-3">
          <ListToolbar
            query={view.query()}
            onQueryChange={view.setQuery}
            placeholder="Filter memories…"
            sortKey={view.sortKey()}
            sortOptions={view.sortOptions}
            onSortChange={view.setSort}
            dir={view.dir()}
            onToggleDir={view.toggleDir}
            count={view.count()}
            total={view.total()}
          />
        </div>

        {/* Loading is driven by the non-suspending `.loading` flag, not <Suspense>:
            the list source reads `memories.latest`, which never suspends. */}
        <Show
          when={!(memories.loading && view.items().length === 0)}
          fallback={
            <div class="p-4">
              <LoadingText />
            </div>
          }
        >
          <Show
            when={view.items().length}
            fallback={
              <EmptyState
                icon="database"
                message="No memories"
                hint={
                  view.isFiltered()
                    ? "No memories match your filter."
                    : "Nothing stored yet. Add a memory or let the assistant learn one."
                }
              />
            }
          >
            <For each={view.items()}>
              {(mem) => (
                <ListRow
                  label={mem.content}
                  leading={mem.pinned ? "lock" : "dot"}
                  right={
                    <span class="flex shrink-0 items-center gap-3">
                      <Show when={mem.hasEmbedding}>
                        <StatusFlag status="info">Emb</StatusFlag>
                      </Show>
                      <Text variant="micro" tone="dim">
                        {relativeTime(mem.createdAt)}
                      </Text>
                      <Menu
                        trigger={
                          <span class="px-1 text-dim hover:text-bright">
                            <Text variant="micro">···</Text>
                          </span>
                        }
                        items={[
                          {
                            label: mem.pinned ? "Unpin" : "PIN",
                            icon: "lock",
                            onSelect: () => handlePin(mem),
                          },
                          {
                            label: "Edit",
                            icon: "edit",
                            onSelect: () => openEdit(mem),
                          },
                          {
                            label: "Delete",
                            icon: "trash",
                            danger: true,
                            onSelect: () => handleDelete(mem),
                          },
                        ]}
                      />
                    </span>
                  }
                />
              )}
            </For>
          </Show>
        </Show>
      </Panel>

      {/* Add / edit memory */}
      <Modal
        open={composeOpen()}
        onClose={() => setComposeOpen(false)}
        title={editing() ? "Edit memory" : "Add memory"}
      >
        <Stack gap={3}>
          <Textarea
            label="Content"
            value={draft()}
            onInput={(e) => setDraft(e.currentTarget.value)}
            placeholder="A fact, preference, or piece of context to remember…"
            rows={5}
          />
          <Checkbox
            label="PINNED — always included in recall"
            checked={draftPinned()}
            onChange={setDraftPinned}
          />
          <div class="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setComposeOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!draft().trim() || saving()}
              onClick={saveDraft}
            >
              {saving() ? "Saving…" : "Save"}
            </Button>
          </div>
        </Stack>
      </Modal>

      {/* Recall */}
      <Modal
        open={recallOpen()}
        onClose={() => setRecallOpen(false)}
        title="Recall"
        class="max-w-2xl"
      >
        <Stack gap={4}>
          <Row gap={2} align="end">
            <div class="flex-1">
              <Input
                label="Query"
                value={recallQuery()}
                onInput={(e) => setRecallQuery(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void runRecall();
                }}
                placeholder="What should the assistant recall?"
              />
            </div>
            <Button
              variant="primary"
              disabled={!recallQuery().trim() || recalling()}
              onClick={runRecall}
            >
              {recalling() ? "Recalling…" : "Recall"}
            </Button>
          </Row>
          <Show when={recallHits()}>
            <Show
              when={recallHits()!.length}
              fallback={
                <EmptyState
                  icon="search"
                  message="No hits"
                  hint="Nothing recalled for that query."
                />
              }
            >
              <Stack gap={2}>
                <For each={recallHits()!}>
                  {(hit) => (
                    <div class="rounded-panel bg-surface p-3 shadow-1">
                      <Row justify="between" align="center">
                        <StatusFlag status="info">
                          {hit.matchedBy.toUpperCase()}
                        </StatusFlag>
                        <Text variant="micro" tone="dim">
                          SCORE {num(hit.score, 3)}
                        </Text>
                      </Row>
                      <Text variant="body" class="mt-2">
                        {hit.content}
                      </Text>
                    </div>
                  )}
                </For>
              </Stack>
            </Show>
          </Show>
        </Stack>
      </Modal>

      {/* Dedup audit */}
      <Modal
        open={dedupOpen()}
        onClose={() => setDedupOpen(false)}
        title="Dedup audit"
        class="max-w-2xl"
        footer={
          <Button variant="ghost" onClick={() => setDedupOpen(false)}>
            Close
          </Button>
        }
      >
        <Show
          when={!auditing()}
          fallback={
            <div class="p-4">
              <LoadingText label="Auditing…" />
            </div>
          }
        >
          <Show
            when={(groups() ?? []).length}
            fallback={
              <EmptyState
                icon="check"
                message="No duplicates found"
                hint="All memories appear distinct."
              />
            }
          >
            <Stack gap={4}>
              <Text variant="body" tone="dim">
                {(groups() ?? []).length} near-duplicate cluster(s) detected.
                Delete the redundant members.
              </Text>
              <For each={groups() ?? []}>
                {(group) => (
                  <div class="overflow-hidden rounded-panel bg-surface shadow-1">
                    <div class="flex items-center justify-between bg-raised px-4 py-2">
                      <Text variant="label" tone="dim">
                        Similarity
                      </Text>
                      <Text variant="readout" tone="warn">
                        {num(group.similarity * 100, 0)}%
                      </Text>
                    </div>
                    <Stack gap={2} class="p-4">
                      <For each={group.memories}>
                        {(m, i) => (
                          <>
                            <Show when={i() > 0}>
                              <Divider />
                            </Show>
                            <Row justify="between" align="start" gap={3}>
                              <Text variant="body" class="min-w-0 break-words">
                                {m.content}
                              </Text>
                              <Button
                                variant="danger"
                                size="sm"
                                leading="trash"
                                onClick={() => deleteFromGroup(m)}
                              >
                                Delete
                              </Button>
                            </Row>
                          </>
                        )}
                      </For>
                    </Stack>
                  </div>
                )}
              </For>
            </Stack>
          </Show>
        </Show>
      </Modal>
    </Stack>
  );
}
