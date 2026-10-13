import { createMemo, createSignal, For, Show, type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import {
  Button,
  Chip,
  Field,
  Icon,
  InstrumentBand,
  PageHeader,
  Panel,
  Resource,
  Row,
  Stack,
  StatusFlag,
  Text,
  Toggle,
  confirm,
  copyToClipboard,
  toast,
} from "~/ui";
import { relativeTime, timestamp } from "~/lib/format";
import { openConversation } from "~/features/chat/data";
import {
  approveTask,
  deleteTask,
  refreshTasks,
  runTaskNow,
  rotateWebhook,
  taskErrorMessage,
  updateTask,
  useRoutineTemplates,
  useScheduledTasks,
} from "../data";
import { TaskFormModal } from "../components/TaskFormModal";
import { TaskRunHistory } from "../components/TaskRunHistory";
import { agentLabel, humanizeSchedule, isDraft } from "../schedule";
import type { RoutineTemplate, ScheduledTask, TaskKind } from "../model";

const KIND_ICON: Record<TaskKind, "chat" | "bell"> = {
  agent: "chat",
  reminder: "bell",
};

export function TasksScreen(): JSX.Element {
  const tasksResource = useScheduledTasks();
  const navigate = useNavigate();
  const templatesResource = useRoutineTemplates();

  const [expandedId, setExpandedId] = createSignal<string | null>(null);
  const [formOpen, setFormOpen] = createSignal(false);
  const [editingTask, setEditingTask] = createSignal<ScheduledTask | null>(
    null,
  );
  const [template, setTemplate] = createSignal<RoutineTemplate | null>(null);
  const [rotatingId, setRotatingId] = createSignal<string | null>(null);
  const [approvingId, setApprovingId] = createSignal<string | null>(null);

  const tasks = () => tasksResource() ?? [];
  const draftCount = createMemo(() => tasks().filter(isDraft).length);
  const enabledCount = createMemo(
    () => tasks().filter((t) => t.enabled && !isDraft(t)).length,
  );
  const disabledCount = createMemo(
    () => tasks().filter((t) => !t.enabled && !isDraft(t)).length,
  );
  const webhookCount = createMemo(
    () => tasks().filter((t) => t.schedule.type === "webhook").length,
  );

  function openThread(task: ScheduledTask) {
    if (!task.conversationId) return;
    openConversation(task.conversationId);
    navigate("/chat");
  }

  async function approve(task: ScheduledTask) {
    setApprovingId(task.id);
    try {
      await approveTask(task.id);
      toast.success(`"${task.title}" approved.`);
    } catch (err) {
      toast.error(taskErrorMessage(err, "Unable to approve the routine."));
    } finally {
      setApprovingId(null);
    }
  }

  function openNew() {
    setTemplate(null);
    setEditingTask(null);
    setFormOpen(true);
  }

  function applyTemplate(t: RoutineTemplate) {
    setTemplate(t);
    setEditingTask(null);
    setFormOpen(true);
  }

  function openEdit(task: ScheduledTask) {
    setEditingTask(task);
    setFormOpen(true);
  }

  async function toggleEnabled(task: ScheduledTask) {
    if (task.enabled) {
      const ok = await confirm({
        title: `Disable "${task.title}"?`,
        detail: "This stops all future scheduled runs until re-enabled.",
        confirmLabel: "Disable",
        cancelLabel: "Keep enabled",
        tone: "alert",
      });
      if (!ok) return;
    }
    try {
      await updateTask(task.id, { enabled: !task.enabled });
      toast.success(
        task.enabled ? `"${task.title}" disabled.` : `"${task.title}" enabled.`,
      );
    } catch (err) {
      toast.error(taskErrorMessage(err, "Unable to update the routine."));
    }
  }

  async function remove(task: ScheduledTask) {
    const ok = await confirm({
      title: `Delete "${task.title}"?`,
      detail:
        "This removes the routine and stops all future scheduled runs. This cannot be undone.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      tone: "alert",
    });
    if (!ok) return;
    try {
      if (expandedId() === task.id) setExpandedId(null);
      await deleteTask(task.id);
      toast.success(`Routine "${task.title}" deleted.`);
    } catch (err) {
      toast.error(taskErrorMessage(err, "Unable to delete the routine."));
    }
  }

  async function runNow(task: ScheduledTask) {
    try {
      await runTaskNow(task.id);
      toast.success(`"${task.title}" queued to run now.`);
    } catch (err) {
      toast.error(taskErrorMessage(err, "Unable to run the routine now."));
    }
  }

  async function rotate(task: ScheduledTask) {
    const ok = await confirm({
      title: `Rotate webhook for "${task.title}"?`,
      detail:
        "The current URL stops working immediately — update anything that calls it.",
      confirmLabel: "Rotate",
      cancelLabel: "Cancel",
      tone: "alert",
    });
    if (!ok) return;
    setRotatingId(task.id);
    try {
      await rotateWebhook(task.id);
      toast.success(`Webhook rotated for "${task.title}".`);
    } catch (err) {
      toast.error(taskErrorMessage(err, "Unable to rotate the webhook."));
    } finally {
      setRotatingId(null);
    }
  }

  return (
    <Stack gap={6}>
      <PageHeader
        title="Routines"
        subtitle="Scheduled agent runs and reminders, each with its own thread."
        assetId="AUTO-TSK-01.0"
        actions={
          <Button variant="primary" leading="plus" onClick={openNew}>
            New routine
          </Button>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(tasks().length) },
          {
            label: "Drafts",
            value: String(draftCount()),
            tone: draftCount() > 0 ? "warn" : "dim",
          },
          { label: "Enabled", value: String(enabledCount()), tone: "nominal" },
          {
            label: "Disabled",
            value: String(disabledCount()),
            tone: disabledCount() > 0 ? "warn" : "dim",
          },
          { label: "Webhooks", value: String(webhookCount()) },
        ]}
      />

      <Show when={(templatesResource() ?? []).length > 0}>
        <Panel label="From a plugin" flush>
          <For each={templatesResource()}>
            {(t) => (
              <div class="flex items-center gap-3 px-3 py-2">
                <Stack gap={0} class="min-w-0 flex-1">
                  <Text variant="label" tone="bright" class="truncate">
                    {t.title}
                  </Text>
                  <Text variant="micro" tone="dim" class="truncate">
                    {t.plugin} · {humanizeSchedule(t.schedule)} ·{" "}
                    {agentLabel(t.agentName)}
                  </Text>
                </Stack>
                <Button
                  variant="default"
                  size="sm"
                  onClick={() => applyTemplate(t)}
                >
                  Use
                </Button>
              </div>
            )}
          </For>
        </Panel>
      </Show>

      <Resource
        data={tasksResource}
        onRetry={refreshTasks}
        isEmpty={(t) => t.length === 0}
        emptyMessage="No routines"
        emptyHint="No routines configured."
        empty={
          <Button variant="default" onClick={openNew}>
            Create routine
          </Button>
        }
      >
        {(loadedTasks) => (
          <Panel flush>
            <For each={loadedTasks()}>
              {(task) => {
                const expanded = () => expandedId() === task.id;
                return (
                  <div>
                    <div
                      class="flex cursor-pointer items-center gap-3 px-3 py-2 transition-colors hover:bg-raised"
                      classList={{ "bg-raised": expanded() }}
                      onClick={() => setExpandedId(expanded() ? null : task.id)}
                    >
                      <div class="flex min-w-0 flex-1 items-center gap-3">
                        <Icon
                          name={KIND_ICON[task.kind]}
                          size={14}
                          class={task.enabled ? "text-bright" : "text-dim"}
                        />
                        <StatusFlag
                          status={
                            isDraft(task)
                              ? "warn"
                              : task.enabled
                                ? "nominal"
                                : "idle"
                          }
                          dot={task.enabled && !isDraft(task)}
                        >
                          {isDraft(task) ? "DRAFT" : task.kind.toUpperCase()}
                        </StatusFlag>
                        <Text
                          variant="label"
                          tone={task.enabled ? "bright" : "dim"}
                          class="truncate"
                        >
                          {task.title}
                        </Text>
                      </div>
                      <div class="hidden items-center gap-4 md:flex">
                        <Text variant="micro" tone="dim" class="font-mono">
                          {humanizeSchedule(task.schedule)}
                        </Text>
                        <Show when={task.kind === "agent"}>
                          <Text variant="micro" tone="dim">
                            {agentLabel(task.agentName)}
                          </Text>
                        </Show>
                        <Show when={task.upcoming[0]}>
                          {(next) => (
                            <Text variant="micro" tone="dim">
                              {relativeTime(next())}
                            </Text>
                          )}
                        </Show>
                        <Show when={task.kind === "agent"}>
                          <StatusFlag
                            status={task.enabled ? "nominal" : "idle"}
                          >
                            {task.output.toUpperCase()}
                          </StatusFlag>
                        </Show>
                      </div>
                      <div
                        class="flex items-center gap-2"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Button
                          variant="ghost"
                          size="sm"
                          leading="play"
                          disabled={isDraft(task)}
                          onClick={() => void runNow(task)}
                        >
                          Run now
                        </Button>
                        <Toggle
                          checked={task.enabled}
                          disabled={isDraft(task)}
                          onChange={() => void toggleEnabled(task)}
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          leading="edit"
                          onClick={(e) => {
                            e.stopPropagation();
                            openEdit(task);
                          }}
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          leading="trash"
                          onClick={(e) => {
                            e.stopPropagation();
                            void remove(task);
                          }}
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          leading={
                            expanded() ? "chevron-down" : "chevron-right"
                          }
                        />
                      </div>
                    </div>

                    <Show when={expanded()}>
                      <div class="bg-raised px-4 py-3">
                        <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
                          <Stack gap={3}>
                            <Show when={isDraft(task)}>
                              <Row gap={3} align="center" class="flex-wrap">
                                <StatusFlag status="warn" dot>
                                  Draft — waiting for your approval
                                </StatusFlag>
                                <Button
                                  variant="primary"
                                  size="sm"
                                  leading="check"
                                  disabled={approvingId() === task.id}
                                  onClick={() => void approve(task)}
                                >
                                  Approve
                                </Button>
                              </Row>
                            </Show>
                            <Field
                              label={
                                task.kind === "agent"
                                  ? "Prompt"
                                  : "Reminder text"
                              }
                              value={task.prompt}
                            />
                            <Row gap={4}>
                              <Field
                                label="Kind"
                                value={task.kind.toUpperCase()}
                              />
                              <Field
                                label="Schedule"
                                value={humanizeSchedule(task.schedule)}
                              />
                              <Show when={task.kind === "agent"}>
                                <Field
                                  label="Agent"
                                  value={agentLabel(task.agentName)}
                                />
                                <Field
                                  label="Output"
                                  value={task.output.toUpperCase()}
                                />
                              </Show>
                            </Row>
                            <Show when={task.preAuthorized.length > 0}>
                              <Stack gap={1}>
                                <Text variant="label" tone="dim">
                                  Pre-authorized
                                </Text>
                                <Row gap={2} class="flex-wrap">
                                  <For each={task.preAuthorized}>
                                    {(scope) => <Chip>{scope}</Chip>}
                                  </For>
                                </Row>
                              </Stack>
                            </Show>
                            <Show when={task.upcoming.length > 0}>
                              <Stack gap={1}>
                                <Text variant="label" tone="dim">
                                  Upcoming
                                </Text>
                                <For each={task.upcoming}>
                                  {(at) => (
                                    <Text
                                      variant="micro"
                                      tone="dim"
                                      class="font-mono"
                                    >
                                      {timestamp(at)}
                                    </Text>
                                  )}
                                </For>
                              </Stack>
                            </Show>
                            <Show when={task.lastRunAt}>
                              <Field
                                label="Last run"
                                value={timestamp(task.lastRunAt!)}
                              />
                            </Show>
                            <Show when={task.conversationId}>
                              <div>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  leading="chat"
                                  onClick={() => openThread(task)}
                                >
                                  Open thread
                                </Button>
                              </div>
                            </Show>
                            <Show when={task.schedule.type === "webhook"}>
                              <Stack gap={1}>
                                <Text variant="label" tone="dim">
                                  Webhook URL
                                </Text>
                                <Row gap={2} align="center" class="flex-wrap">
                                  <Text
                                    variant="micro"
                                    tone="dim"
                                    class="break-all font-mono"
                                  >
                                    {task.webhookUrl ?? "—"}
                                  </Text>
                                  <Show when={task.webhookUrl}>
                                    <Button
                                      variant="ghost"
                                      size="sm"
                                      leading="copy"
                                      onClick={() =>
                                        copyToClipboard(
                                          task.webhookUrl!,
                                          "Webhook URL",
                                        )
                                      }
                                    >
                                      Copy
                                    </Button>
                                  </Show>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    leading="refresh"
                                    disabled={rotatingId() === task.id}
                                    onClick={() => void rotate(task)}
                                  >
                                    Rotate
                                  </Button>
                                </Row>
                              </Stack>
                            </Show>
                          </Stack>

                          <TaskRunHistory taskId={task.id} />
                        </div>
                      </div>
                    </Show>
                  </div>
                );
              }}
            </For>
          </Panel>
        )}
      </Resource>

      <TaskFormModal
        open={formOpen()}
        onClose={() => setFormOpen(false)}
        task={editingTask()}
        template={template()}
      />
    </Stack>
  );
}
