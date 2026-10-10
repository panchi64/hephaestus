import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  EmptyState,
  HIDDEN_FILE_INPUT,
  InfoHint,
  InstrumentBand,
  ListRow,
  ListToolbar,
  LoadingText,
  Menu,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Tabs,
  Text,
  confirm,
  toast,
  useFileDrop,
} from "~/ui";
import { createListView } from "~/lib/list";
import { useTabParam } from "~/lib/useTabParam";
import { bytes, relativeTime } from "~/lib/format";
import {
  deleteSkill,
  exportSkill,
  importSkill,
  setSkillPublished,
  skillErrorMessage,
  useSkills,
} from "../data";
import {
  skillSourceFlag,
  skillStatusFlag,
  skillStatusLabel,
  type SkillSummary,
} from "../model";
import { NewSkillDialog } from "../components/NewSkillDialog";

const STATUS_TABS = [
  { value: "all", label: "All" },
  { value: "published", label: "Published" },
  { value: "draft", label: "Draft" },
];
const STATUS_VALUES = ["all", "published", "draft"] as const;
type StatusFilter = (typeof STATUS_VALUES)[number];

export function SkillsDirectoryScreen(props: {
  /** Open a skill's editor. The directory used to navigate to `/skills/{id}`;
   *  it now lives inside the settings dialog, which owns that second level, so
   *  where "open" goes is the caller's business rather than a route this
   *  component hard-codes. */
  onOpen: (id: string) => void;
}): JSX.Element {
  const skillsResource = useSkills();
  // Reading a Solid resource accessor re-throws its error (same hazard GalleryScreen
  // documents). A 500 from /skills would otherwise trip the shell's ErrorBoundary and
  // replace the whole page with one message — InstrumentBand reads this and sits outside
  // the Suspense. Derive from `settled` so a failed load degrades to an empty
  // library with a message, and the boundary stays the net rather than the plan.
  const skills = (): SkillSummary[] => settled(skillsResource) ?? [];
  const loadError = (): string | null =>
    skillsResource.error
      ? skillErrorMessage(skillsResource.error, "Could not load skills.")
      : null;
  const [statusFilter, setStatusFilter] = useTabParam<StatusFilter>(
    "tab",
    STATUS_VALUES,
    "all",
  );
  const [newOpen, setNewOpen] = createSignal(false);
  const [importing, setImporting] = createSignal(false);

  // ── import ──────────────────────────────────────────────────────────────

  /** The endpoint takes one bundle per call, so only the first pick is sent. */
  const picker = useFileDrop((files) => void handleImport(files[0]));

  async function handleImport(file: File | undefined): Promise<void> {
    if (!file || importing()) return;
    setImporting(true);
    try {
      const { skill, warnings } = await importSkill(file);
      toast.success(`Imported "${skill.name}" as a draft`, {
        action: {
          label: "Open",
          onClick: () => props.onOpen(skill.id),
        },
      });
      // Everything the backend flagged about the bundle, verbatim — the operator
      // reads these before deciding to publish.
      for (const warning of warnings) toast.warn(warning);
    } catch (err) {
      toast.error(skillErrorMessage(err, "Could not import the bundle."));
    } finally {
      setImporting(false);
    }
  }

  // ── row actions ─────────────────────────────────────────────────────────

  async function handleDelete(skill: SkillSummary): Promise<void> {
    const ok = await confirm({
      title: `Delete "${skill.name}"?`,
      detail:
        "The skill and every file in its bundle are permanently removed. Export it first if you want a copy.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteSkill(skill.id);
      toast.success(`Deleted "${skill.name}"`);
    } catch (err) {
      toast.error(skillErrorMessage(err, `Could not delete "${skill.name}"`));
    }
  }

  async function handlePublishToggle(skill: SkillSummary): Promise<void> {
    const next = !skill.published;
    try {
      await setSkillPublished(skill.id, next);
    } catch (err) {
      toast.error(
        skillErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} "${skill.name}"`,
        ),
      );
      return;
    }
    toast.success(
      next
        ? `"${skill.name}" published — the agent can see it now.`
        : `"${skill.name}" unpublished — back to draft.`,
      {
        action: {
          label: "Undo",
          onClick: () => {
            setSkillPublished(skill.id, !next).catch(() =>
              toast.error("Could not undo"),
            );
          },
        },
      },
    );
  }

  async function handleExport(skill: SkillSummary): Promise<void> {
    try {
      await exportSkill(skill.id, skill.name);
    } catch (err) {
      toast.error(skillErrorMessage(err, `Could not export "${skill.name}"`));
    }
  }

  // ── derived ─────────────────────────────────────────────────────────────

  const publishedCount = () => skills().filter((s) => s.published).length;
  const draftCount = () => skills().length - publishedCount();
  const totalBytes = () => skills().reduce((sum, s) => sum + s.sizeBytes, 0);

  const inTab = () => {
    const f = statusFilter();
    if (f === "all") return skills();
    return skills().filter((s) => s.published === (f === "published"));
  };

  /** Why the list is empty, when the load itself succeeded. */
  const emptyHint = (): string => {
    if (view.isFiltered()) return "No skills match your search.";
    return statusFilter() === "all"
      ? "Create a skill or import an Agent Skills bundle."
      : "No skills match the current filter.";
  };

  const view = createListView({
    source: inTab,
    search: (s) => `${s.name} ${s.description}`,
    sorts: {
      recent: {
        label: "Newest",
        compare: (a, b) => a.updatedAt.localeCompare(b.updatedAt),
      },
      name: {
        label: "Name",
        compare: (a, b) => a.name.localeCompare(b.name),
      },
    },
    initialSort: "recent",
    initialDir: "desc",
  });

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Skills"
        subtitle="Agent Skills bundles — reusable procedures the assistant can follow."
        assetId="ODY-SKL-01.0"
        actions={
          <Row gap={2}>
            <input
              ref={picker.bindInput}
              {...HIDDEN_FILE_INPUT}
              multiple={false}
              accept=".zip,.md"
              {...picker.inputHandlers}
            />
            <Button
              variant="default"
              leading="upload"
              disabled={importing()}
              onClick={picker.openPicker}
            >
              {importing() ? "Importing…" : "Import"}
            </Button>
            <Button
              variant="primary"
              leading="plus"
              onClick={() => setNewOpen(true)}
            >
              New skill
            </Button>
          </Row>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(skills().length) },
          {
            label: "Published",
            value: String(publishedCount()),
            tone: "nominal",
          },
          { label: "Draft", value: String(draftCount()), tone: "dim" },
          { label: "Bundles", value: bytes(totalBytes()) },
        ]}
      />

      <Panel flush>
        <div class="flex items-center justify-between gap-3 pr-3">
          <Tabs
            items={STATUS_TABS}
            value={statusFilter()}
            onChange={(v) => setStatusFilter(v as StatusFilter)}
          />
          <Row align="center" gap={3}>
            <Row align="center" gap={1}>
              <Text variant="micro" tone="dim">
                Draft
              </Text>
              <InfoHint label="A draft is invisible to the agent. Publishing is what makes a skill's instructions something the assistant will follow — imported bundles always land as drafts so you can read them first." />
            </Row>
            <Row align="center" gap={1}>
              <Text variant="micro" tone="dim">
                Import
              </Text>
              <InfoHint label="Accepts an Agent Skills bundle (.zip) or a lone SKILL.md. The bundle keeps its supporting files, and anything unusual about it is reported as a warning." />
            </Row>
          </Row>
        </div>

        <div class="p-3">
          <ListToolbar
            query={view.query()}
            onQueryChange={view.setQuery}
            placeholder="Search by name or description…"
            sortKey={view.sortKey()}
            sortOptions={view.sortOptions}
            onSortChange={view.setSort}
            dir={view.dir()}
            onToggleDir={view.toggleDir}
            count={view.count()}
            total={view.total()}
          />
        </div>

        <Suspense
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
                icon="layers"
                message={loadError() ? "Skills unavailable" : "No skills"}
                hint={loadError() ?? emptyHint()}
              />
            }
          >
            <For each={view.items()}>
              {(skill) => (
                <ListRow
                  label={skill.name}
                  leading="layers"
                  onClick={() => props.onOpen(skill.id)}
                  right={
                    <span class="flex shrink-0 items-center gap-3">
                      {/* Shown for any bundle that has files at all — a row that
                          drops the field at one file would make the column jump. */}
                      <Show when={skill.fileCount > 0}>
                        <Text variant="micro" tone="dim">
                          {skill.fileCount}{" "}
                          {skill.fileCount === 1 ? "File" : "Files"} ·{" "}
                          {bytes(skill.sizeBytes)}
                        </Text>
                      </Show>
                      <Show when={skill.source !== "authored"}>
                        <StatusFlag status={skillSourceFlag[skill.source]}>
                          {skill.source.toUpperCase()}
                        </StatusFlag>
                      </Show>
                      <StatusFlag status={skillStatusFlag(skill.published)}>
                        {skillStatusLabel(skill.published)}
                      </StatusFlag>
                      <Text variant="micro" tone="dim">
                        {relativeTime(skill.updatedAt)}
                      </Text>
                      {/* Stop row navigation when interacting with the menu. */}
                      <span
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                        }}
                      >
                        <Menu
                          trigger={
                            <span class="px-1 text-dim hover:text-bright">
                              <Text variant="micro">···</Text>
                            </span>
                          }
                          items={[
                            {
                              label: "Edit",
                              icon: "edit",
                              onSelect: () => props.onOpen(skill.id),
                            },
                            {
                              label: "Export",
                              icon: "download",
                              onSelect: () => void handleExport(skill),
                            },
                            {
                              label: skill.published ? "Unpublish" : "Publish",
                              icon: "check",
                              onSelect: () => void handlePublishToggle(skill),
                            },
                            {
                              label: "Delete",
                              icon: "trash",
                              danger: true,
                              onSelect: () => void handleDelete(skill),
                            },
                          ]}
                        />
                      </span>
                    </span>
                  }
                />
              )}
            </For>
          </Show>
        </Suspense>
      </Panel>

      <NewSkillDialog
        open={newOpen()}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => {
          setNewOpen(false);
          props.onOpen(id);
        }}
      />
    </Stack>
  );
}
