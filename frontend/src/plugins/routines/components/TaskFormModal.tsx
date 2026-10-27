import { createEffect, createMemo, Show, For, type JSX } from "solid-js";
import { createStore } from "solid-js/store";
import {
  Button,
  Checkbox,
  Input,
  Modal,
  Row,
  Select,
  Stack,
  Text,
  Textarea,
  toast,
} from "~/ui";
import { timestamp } from "~/lib/format";
import { pickableAgents, useAgents } from "~/features/agents";
import {
  createTask,
  parseSchedule,
  taskErrorMessage,
  updateTask,
  useApprovalScopes,
  type TaskInput,
} from "../data";
import type {
  OutputChannel,
  RoutineTemplate,
  ScheduledTask,
  ScheduleType,
  TaskKind,
} from "../model";
import {
  agentFromValue,
  agentOptions,
  browserTimezone,
  CRON_PATTERN,
  isoToLocalInput,
  secondsToValueUnit,
  DEFAULT_AGENT_VALUE,
  UNIT_SECONDS,
  type IntervalUnit,
} from "../schedule";

interface FormState {
  kind: TaskKind;
  title: string;
  prompt: string;
  scheduleType: ScheduleType;
  runAtLocal: string;
  intervalValue: string;
  intervalUnit: IntervalUnit;
  cron: string;
  timezone: string;
  phrase: string;
  parsing: boolean;
  parseError: string;
  upcoming: string[];
  output: OutputChannel;
  agent: string;
  scopes: Set<string>;
  titleError: string;
  scheduleError: string;
}

function blankForm(): FormState {
  return {
    kind: "agent",
    title: "",
    prompt: "",
    scheduleType: "cron",
    runAtLocal: "",
    intervalValue: "1",
    intervalUnit: "hours",
    cron: "",
    timezone: browserTimezone(),
    phrase: "",
    parsing: false,
    parseError: "",
    upcoming: [],
    output: "chat",
    agent: DEFAULT_AGENT_VALUE,
    scopes: new Set(),
    titleError: "",
    scheduleError: "",
  };
}

function formFromTask(task: ScheduledTask): FormState {
  const sched = task.schedule;
  const interval =
    sched.type === "interval" && sched.everySeconds
      ? secondsToValueUnit(sched.everySeconds)
      : { value: "1", unit: "hours" as IntervalUnit };
  return {
    kind: task.kind,
    title: task.title,
    prompt: task.prompt,
    scheduleType: sched.type,
    runAtLocal: sched.runAt ? isoToLocalInput(sched.runAt) : "",
    intervalValue: interval.value,
    intervalUnit: interval.unit,
    cron: sched.cron ?? "",
    timezone: sched.timezone || browserTimezone(),
    phrase: "",
    parsing: false,
    parseError: "",
    upcoming: task.upcoming,
    output: task.output,
    agent: task.agentName ?? DEFAULT_AGENT_VALUE,
    scopes: new Set(task.preAuthorized),
    titleError: "",
    scheduleError: "",
  };
}

function formFromTemplate(template: RoutineTemplate): FormState {
  return formFromTask({
    id: "",
    kind: "agent",
    title: template.title,
    prompt: template.prompt,
    schedule: template.schedule,
    output: "chat",
    agentName: template.agentName,
    approved: true,
    conversationId: null,
    upcoming: [],
    preAuthorized: [],
    enabled: true,
    createdAt: "",
    updatedAt: "",
  });
}

/** Create/edit modal for a routine — the kind toggle (agent/reminder), agent
 *  picker, schedule builder (a described phrase read by the backend, or
 *  once/interval/cron/webhook fields), output channel, and pre-authorization
 *  scope checkboxes. A `reminder` preset hides the agent-only fields (agent,
 *  output channel, pre-authorized scopes): a reminder never drives a Run, so
 *  none has meaning for it. */
export function TaskFormModal(props: {
  open: boolean;
  onClose: () => void;
  task: ScheduledTask | null;
  /** Prefills a new routine; nothing is saved until the form is submitted. */
  template?: RoutineTemplate | null;
}): JSX.Element {
  const [form, setForm] = createStore<FormState>(blankForm());
  // Which tools can be pre-authorized is the backend's to decide — it depends on
  // what the operator has registered, so the form asks rather than assumes.
  const scopes = useApprovalScopes();
  const agents = useAgents();
  const agentChoices = createMemo(() =>
    agentOptions(
      pickableAgents(agents() ?? [])
        .map((a) => a.name)
        .filter((name) => name !== "default"),
      props.task?.agentName ?? null,
    ),
  );

  createEffect(() => {
    if (!props.open) return;
    if (props.task) setForm(formFromTask(props.task));
    else if (props.template) setForm(formFromTemplate(props.template));
    else setForm(blankForm());
  });

  const isAgent = () => form.kind === "agent";
  const isEditing = () => props.task !== null;

  function toggleScope(id: string, checked: boolean) {
    setForm("scopes", (prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function readPhrase() {
    const phrase = form.phrase.trim();
    if (!phrase) return;
    setForm({ parsing: true, parseError: "" });
    try {
      const { schedule, upcoming } = await parseSchedule(phrase, form.timezone);
      const interval = schedule.everySeconds
        ? secondsToValueUnit(schedule.everySeconds)
        : null;
      setForm({
        scheduleType: schedule.type,
        runAtLocal: schedule.runAt ? isoToLocalInput(schedule.runAt) : "",
        intervalValue: interval?.value ?? form.intervalValue,
        intervalUnit: interval?.unit ?? form.intervalUnit,
        cron: schedule.cron ?? "",
        timezone: schedule.timezone || form.timezone,
        scheduleError: "",
        upcoming,
      });
    } catch (err) {
      setForm(
        "parseError",
        taskErrorMessage(err, "Unable to read that schedule."),
      );
    } finally {
      setForm("parsing", false);
    }
  }

  function validate(): boolean {
    let valid = true;
    if (!form.title.trim()) {
      setForm("titleError", "TITLE is required.");
      valid = false;
    } else {
      setForm("titleError", "");
    }

    if (form.scheduleType === "once" && !form.runAtLocal) {
      setForm("scheduleError", "Pick a date and time.");
      valid = false;
    } else if (form.scheduleType === "interval") {
      const n = Number(form.intervalValue);
      if (!Number.isFinite(n) || n <= 0) {
        setForm("scheduleError", "Enter a positive interval.");
        valid = false;
      } else {
        setForm("scheduleError", "");
      }
    } else if (form.scheduleType === "cron") {
      if (!CRON_PATTERN.test(form.cron.trim())) {
        setForm(
          "scheduleError",
          "Invalid cron expression — expected five space-separated fields (e.g. 0 8 * * *).",
        );
        valid = false;
      } else {
        setForm("scheduleError", "");
      }
    } else {
      setForm("scheduleError", "");
    }
    return valid;
  }

  async function save() {
    if (!validate()) return;

    const timezone = form.timezone.trim() || browserTimezone();
    const schedule: TaskInput["schedule"] =
      form.scheduleType === "once"
        ? {
            type: "once",
            runAt: new Date(form.runAtLocal).toISOString(),
            timezone,
          }
        : form.scheduleType === "interval"
          ? {
              type: "interval",
              everySeconds:
                Number(form.intervalValue) * UNIT_SECONDS[form.intervalUnit],
              timezone,
            }
          : form.scheduleType === "cron"
            ? { type: "cron", cron: form.cron.trim(), timezone }
            : { type: "webhook", timezone };

    // `output` only has meaning for an agent routine (which channel to deliver its
    // run to); a reminder always fires a direct notification per the schedule
    // contract regardless of this field, so it's left at the form default
    // rather than implying a real distinction that doesn't exist.
    const input: TaskInput = {
      kind: form.kind,
      title: form.title.trim(),
      prompt: form.prompt,
      schedule,
      output: form.output,
      agentName: isAgent() ? agentFromValue(form.agent) : null,
      preAuthorized: isAgent() ? [...form.scopes] : [],
    };

    try {
      const saved = isEditing()
        ? await updateTask(props.task!.id, input)
        : await createTask(input);
      toast.success(
        isEditing() ? `"${saved.title}" updated.` : `"${saved.title}" created.`,
      );
      props.onClose();
    } catch (err) {
      toast.error(
        taskErrorMessage(
          err,
          isEditing()
            ? "Unable to update the routine."
            : "Unable to create the routine.",
        ),
      );
    }
  }

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title={isEditing() ? "Edit routine" : "New routine"}
      class="max-w-lg"
      footer={
        <Row gap={2}>
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button variant="primary" leading="check" onClick={() => void save()}>
            Save
          </Button>
        </Row>
      }
    >
      <Stack gap={4}>
        <Select
          label="Kind"
          value={form.kind}
          onChange={(v) => setForm("kind", v as TaskKind)}
          disabled={isEditing()}
          hint={
            isEditing() ? "Set at creation — cannot be changed." : undefined
          }
          options={[
            { value: "agent", label: "Agent routine" },
            { value: "reminder", label: "Reminder" },
          ]}
        />
        <Stack gap={1}>
          <Input
            label="TITLE *"
            value={form.title}
            onInput={(e) => {
              setForm("title", e.currentTarget.value);
              if (e.currentTarget.value.trim()) setForm("titleError", "");
            }}
            placeholder="Descriptive routine title"
          />
          <Show when={form.titleError}>
            <Text variant="caption" tone="alert">
              {form.titleError}
            </Text>
          </Show>
        </Stack>
        <Textarea
          label={isAgent() ? "Prompt" : "Reminder text"}
          rows={4}
          value={form.prompt}
          onInput={(e) => setForm("prompt", e.currentTarget.value)}
          hint={
            isAgent()
              ? "Each run gets its own thread, told how the previous run ended."
              : "Delivered verbatim as the notification body — no AI phrasing."
          }
        />

        <Show when={isAgent()}>
          <Select
            label="Agent"
            value={form.agent}
            onChange={(v) => setForm("agent", v)}
            options={agentChoices()}
          />
        </Show>

        <Stack gap={1}>
          <Row gap={2} align="end">
            <Input
              label="Describe the schedule"
              fieldClass="flex-1 min-w-0"
              value={form.phrase}
              onInput={(e) => {
                setForm("phrase", e.currentTarget.value);
                setForm("parseError", "");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void readPhrase();
                }
              }}
              placeholder="every weekday at 8am"
            />
            <Button
              variant="default"
              disabled={form.parsing || !form.phrase.trim()}
              onClick={() => void readPhrase()}
            >
              {form.parsing ? "Reading…" : "Read"}
            </Button>
          </Row>
          <Show when={form.parseError}>
            <Text variant="caption" tone="alert">
              {form.parseError}
            </Text>
          </Show>
        </Stack>

        <Select
          label="Schedule type"
          value={form.scheduleType}
          onChange={(v) => {
            setForm("scheduleType", v as ScheduleType);
            setForm("scheduleError", "");
          }}
          options={[
            { value: "cron", label: "Cron expression" },
            { value: "interval", label: "Recurring interval" },
            { value: "once", label: "One-time" },
            { value: "webhook", label: "Webhook" },
          ]}
        />

        <Show when={form.scheduleType === "once"}>
          <Stack gap={1}>
            <Input
              type="datetime-local"
              label="RUN AT *"
              value={form.runAtLocal}
              onInput={(e) => {
                setForm("runAtLocal", e.currentTarget.value);
                setForm("scheduleError", "");
              }}
            />
            <Show when={form.scheduleError}>
              <Text variant="caption" tone="alert">
                {form.scheduleError}
              </Text>
            </Show>
          </Stack>
        </Show>

        <Show when={form.scheduleType === "interval"}>
          <Row gap={2} align="end">
            <Input
              type="number"
              min="1"
              label="Every *"
              fieldClass="w-24"
              value={form.intervalValue}
              onInput={(e) => {
                setForm("intervalValue", e.currentTarget.value);
                setForm("scheduleError", "");
              }}
            />
            <Select
              value={form.intervalUnit}
              onChange={(v) => setForm("intervalUnit", v as IntervalUnit)}
              options={[
                { value: "seconds", label: "Seconds" },
                { value: "minutes", label: "Minutes" },
                { value: "hours", label: "Hours" },
                { value: "days", label: "Days" },
              ]}
            />
          </Row>
          <Show when={form.scheduleError}>
            <Text variant="caption" tone="alert">
              {form.scheduleError}
            </Text>
          </Show>
        </Show>

        <Show when={form.scheduleType === "cron"}>
          <Stack gap={1}>
            <Input
              label="CRON *"
              value={form.cron}
              onInput={(e) => {
                setForm("cron", e.currentTarget.value);
                setForm("scheduleError", "");
              }}
              placeholder="0 8 * * *"
              hint="Five space-separated fields: minute hour day-of-month month day-of-week."
            />
            <Show when={form.scheduleError}>
              <Text variant="caption" tone="alert">
                {form.scheduleError}
              </Text>
            </Show>
          </Stack>
        </Show>

        <Show when={form.scheduleType === "cron"}>
          <Input
            label="TIMEZONE"
            value={form.timezone}
            onInput={(e) => setForm("timezone", e.currentTarget.value)}
            placeholder="Europe/Paris"
            hint="IANA zone the cron fields are read in."
          />
        </Show>

        <Show when={form.upcoming.length > 0}>
          <Stack gap={1}>
            <Text variant="label" tone="dim">
              Upcoming
            </Text>
            <For each={form.upcoming}>
              {(at) => (
                <Text variant="micro" tone="dim" class="font-mono">
                  {timestamp(at)}
                </Text>
              )}
            </For>
          </Stack>
        </Show>

        <Show when={form.scheduleType === "webhook"}>
          <Text variant="caption" tone="dim">
            Fires only when its webhook URL is called — no time-based schedule.
            The URL is generated once this routine is saved.
          </Text>
        </Show>

        <Show when={isAgent()}>
          <Select
            label="Output channel"
            value={form.output}
            onChange={(v) => setForm("output", v as OutputChannel)}
            options={[
              { value: "chat", label: "Chat (conversation is the artifact)" },
              {
                value: "notification",
                label: "Notification (+ outcome summary)",
              },
            ]}
          />

          <Stack gap={2}>
            <Text variant="label" tone="dim">
              Pre-authorized
            </Text>
            <Text variant="caption" tone="dim">
              Sensitive tool calls in this scope run unattended; anything else
              still parks for approval.
            </Text>
            <Stack gap={2}>
              <For each={scopes() ?? []}>
                {(scope) => (
                  <Stack gap={0}>
                    <Checkbox
                      label={scope.name}
                      checked={form.scopes.has(scope.name)}
                      onChange={(checked) => toggleScope(scope.name, checked)}
                    />
                    <Text variant="caption" tone="dim" class="pl-6">
                      {scope.description}
                    </Text>
                  </Stack>
                )}
              </For>
              <Show when={scopes()?.length === 0}>
                <Text variant="caption" tone="dim">
                  No tools in this workspace pause for approval.
                </Text>
              </Show>
            </Stack>
          </Stack>
        </Show>
      </Stack>
    </Modal>
  );
}
