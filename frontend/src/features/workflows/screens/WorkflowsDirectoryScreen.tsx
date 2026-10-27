import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  EmptyState,
  InfoHint,
  InstrumentBand,
  ListRow,
  ListToolbar,
  LoadingText,
  Menu,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  confirm,
  toast,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { createListView } from "~/lib/list";
import { relativeTime } from "~/lib/format";
import {
  deleteWorkflow,
  updateWorkflow,
  useWorkflows,
  workflowErrorMessage,
} from "../data";
import {
  workflowStatusFlag,
  workflowStatusLabel,
  type Workflow,
} from "../model";
import { NewWorkflowDialog } from "../components/NewWorkflowDialog";

export function WorkflowsDirectoryScreen(props: {
  /** Open a workflow's editor. Where "open" goes is the caller's business rather than a
   *  route this component hard-codes — inside the settings dialog there is none. */
  onOpen: (id: string) => void;
}): JSX.Element {
  const workflowsResource = useWorkflows();
  // Reading a Solid resource accessor re-throws its error. A 500 here would otherwise
  // trip the shell's ErrorBoundary and replace the whole dialog with one message —
  // InstrumentBand reads this and sits outside the Suspense. Derive from
  // `settled` so a failed load degrades to an empty list with a message, and
  // the boundary stays the net rather than the plan.
  const workflows = (): Workflow[] => settled(workflowsResource) ?? [];
  const loadError = (): string | null =>
    workflowsResource.error
      ? workflowErrorMessage(
          workflowsResource.error,
          "Could not load workflows.",
        )
      : null;
  const [newOpen, setNewOpen] = createSignal(false);

  async function handleDelete(workflow: Workflow): Promise<void> {
    const ok = await confirm({
      title: `Delete "/${workflow.name}"?`,
      detail:
        "The template is permanently removed and the name stops working in the composer. Turning it off instead keeps it here.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteWorkflow(workflow.id);
      toast.success(`Deleted "/${workflow.name}"`);
    } catch (err) {
      toast.error(
        workflowErrorMessage(err, `Could not delete "/${workflow.name}"`),
      );
    }
  }

  async function handleEnabledToggle(workflow: Workflow): Promise<void> {
    const next = !workflow.enabled;
    try {
      await updateWorkflow(workflow.id, { enabled: next });
    } catch (err) {
      toast.error(
        workflowErrorMessage(
          err,
          `Could not turn "/${workflow.name}" ${next ? "on" : "off"}`,
        ),
      );
      return;
    }
    toast.success(
      next
        ? `"/${workflow.name}" is in the menu again.`
        : `"/${workflow.name}" is out of the menu — still here, not offered.`,
      {
        action: {
          label: "Undo",
          onClick: () => {
            updateWorkflow(workflow.id, { enabled: !next }).catch(() =>
              toast.error("Could not undo"),
            );
          },
        },
      },
    );
  }

  const enabledCount = () => workflows().filter((w) => w.enabled).length;

  const emptyHint = (): string =>
    view.isFiltered()
      ? "No workflows match your search."
      : "Save a prompt you write often, and reach it by typing its name.";

  const view = createListView({
    source: workflows,
    search: (w) => `${w.name} ${w.title} ${w.description}`,
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
      <SettingsPageHeader
        section="workflows"
        actions={
          <Button
            variant="primary"
            leading="plus"
            onClick={() => setNewOpen(true)}
          >
            New workflow
          </Button>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(workflows().length) },
          { label: "In the menu", value: String(enabledCount()) },
          {
            label: "Off",
            value: String(workflows().length - enabledCount()),
            tone: "dim",
          },
        ]}
      />

      <Panel flush>
        <div class="flex items-center justify-end gap-3 px-3 pt-3">
          <Row align="center" gap={1}>
            <Text variant="micro" tone="dim">
              Who sees these
            </Text>
            <InfoHint label="Only you. A workflow is reached by typing its name in the composer — unlike a skill, the agent is never shown the list and cannot invoke one on its own." />
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
                icon="terminal"
                message={loadError() ? "Workflows unavailable" : "No workflows"}
                hint={loadError() ?? emptyHint()}
              />
            }
          >
            <For each={view.items()}>
              {(workflow) => (
                <ListRow
                  // The slash is part of what the operator is looking at: this row is a
                  // thing they type, not a record they own.
                  label={`/${workflow.name}`}
                  leading="terminal"
                  onClick={() => props.onOpen(workflow.id)}
                  right={
                    <span class="flex shrink-0 items-center gap-3">
                      <Show when={workflow.description}>
                        <Text variant="micro" tone="dim">
                          {workflow.description}
                        </Text>
                      </Show>
                      <StatusFlag status={workflowStatusFlag(workflow.enabled)}>
                        {workflowStatusLabel(workflow.enabled)}
                      </StatusFlag>
                      <Text variant="micro" tone="dim">
                        {relativeTime(workflow.updatedAt)}
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
                              onSelect: () => props.onOpen(workflow.id),
                            },
                            {
                              label: workflow.enabled
                                ? "Take out of the menu"
                                : "Put back in the menu",
                              icon: "check",
                              onSelect: () =>
                                void handleEnabledToggle(workflow),
                            },
                            {
                              label: "Delete",
                              icon: "trash",
                              danger: true,
                              onSelect: () => void handleDelete(workflow),
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

      <NewWorkflowDialog
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
