import { createResource, createSignal, type Resource } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { apiUrl } from "~/lib/config";
import type {
  ApprovalScope,
  OutputChannel,
  ParsedSchedule,
  RoutineTemplate,
  ScheduledTask,
  TaskKind,
  TaskRun,
  TaskSchedule,
} from "./model";

/** `ScheduledTask` DTO as the backend actually sends it — identical to the seam
 *  type except `webhookUrl` arrives as a path relative to the API origin. */
interface TaskOut extends Omit<ScheduledTask, "webhookUrl"> {
  webhookUrl?: string;
}

/** Compose the absolute, ready-to-copy webhook URL from the backend's relative
 *  path (present only on `webhook`-type tasks). */
function toTask(dto: TaskOut): ScheduledTask {
  return {
    ...dto,
    webhookUrl: dto.webhookUrl ? apiUrl(dto.webhookUrl) : undefined,
  };
}

/* ── List (the seam) ──────────────────────────────────────────────────────── */

const [listTick, setListTick] = createSignal(0);

async function fetchTasks(): Promise<ScheduledTask[]> {
  const { items } = await api.get<{ items: TaskOut[] }>("/tasks");
  return items.map(toTask);
}

export function useScheduledTasks(): Resource<ScheduledTask[]> {
  const [data] = createResource(listTick, fetchTasks);
  return data;
}

/** Invalidate the list after a mutation. */
export function refreshTasks(): void {
  setListTick((n) => n + 1);
}

/* ── Plugin templates ─────────────────────────────────────────────────────── */

/** Routine templates offered by published plugins. */
export function useRoutineTemplates(): Resource<RoutineTemplate[]> {
  const [data] = createResource(async () => {
    const { items } = await api.get<{ items: RoutineTemplate[] }>(
      "/tasks/templates",
    );
    return items;
  });
  return data;
}

/* ── Pre-authorization scopes ─────────────────────────────────────────────── */

/** The tools this workspace can pause a run for, as the backend derives them.
 *
 *  Fetched, never listed here: the operator's external tools are named from the
 *  MCP servers and connectors *they* registered, so the set only exists at
 *  runtime. A hard-coded list would offer scopes that don't apply and silently
 *  omit every tool added after it was written — which is exactly what the list
 *  this replaced had done. */
export function useApprovalScopes(): Resource<ApprovalScope[]> {
  const [data] = createResource(async () =>
    api.get<ApprovalScope[]>("/tools/approval-scopes"),
  );
  return data;
}

/* ── Run history (per task, fetched lazily on expand) ─────────────────────── */

const RUNS_LIMIT = 50;

/** Bumped after `runTaskNow` so an open run-history panel picks up the new
 *  (still-live) run without the operator having to collapse/reopen the row. */
const [runsTick, setRunsTick] = createSignal(0);

/** A task's own run history — kept separate from the list resource (unlike the
 *  Phase-1 mock's single flat `TaskRun[]`) because the real endpoint is
 *  per-task (`GET /tasks/{id}/runs`); the screen fetches it when a row expands,
 *  the same lazy-per-id pattern `ConversationGrants` uses for grants. */
export function useTaskRuns(taskId: () => string | null): Resource<TaskRun[]> {
  const [data] = createResource(
    () => ({ id: taskId(), tick: runsTick() }),
    async (src) => {
      if (!src.id) return [];
      const { items } = await api.get<{ items: TaskRun[] }>(
        `/tasks/${src.id}/runs?limit=${RUNS_LIMIT}`,
      );
      return items;
    },
  );
  return data;
}

/* ── Mutations ─────────────────────────────────────────────────────────────── */

export interface TaskInput {
  kind: TaskKind;
  title: string;
  prompt: string;
  schedule: TaskSchedule;
  output: OutputChannel;
  agentName: string | null;
  preAuthorized: string[];
}

export async function createTask(input: TaskInput): Promise<ScheduledTask> {
  const dto = await api.post<TaskOut>("/tasks", input);
  refreshTasks();
  return toTask(dto);
}

export type TaskPatch = Partial<TaskInput> & { enabled?: boolean };

export async function updateTask(
  id: string,
  patch: TaskPatch,
): Promise<ScheduledTask> {
  const dto = await api.patch<TaskOut>(`/tasks/${id}`, patch);
  refreshTasks();
  return toTask(dto);
}

export async function deleteTask(id: string): Promise<void> {
  await api.del(`/tasks/${id}`);
  refreshTasks();
}

export async function runTaskNow(id: string): Promise<{ taskRunId: string }> {
  const res = await api.post<{ taskRunId: string }>(`/tasks/${id}/run_now`);
  setRunsTick((n) => n + 1);
  refreshTasks(); // picks up the new lastRunAt/nextRunAt once it settles
  return res;
}

/** Rotate a webhook task's credential — the unguessable token is the sole
 *  credential (there is no separate secret/signature), so rotation is just a
 *  targeted PATCH that asks the backend to replace it and hand back the new
 *  URL. Only meaningful for `schedule.type === "webhook"` tasks. `rotateWebhookToken`
 *  is a one-shot action flag on `PATCH /tasks/{id}` (`routes/tasks.py`'s
 *  `TaskPatch.rotate_webhook_token`), not a stored field. */
export async function rotateWebhook(id: string): Promise<ScheduledTask> {
  const dto = await api.patch<TaskOut>(`/tasks/${id}`, {
    rotateWebhookToken: true,
  });
  refreshTasks();
  return toTask(dto);
}

/** Publish an agent-drafted routine so it starts firing. */
export async function approveTask(id: string): Promise<ScheduledTask> {
  const dto = await api.post<TaskOut>(`/tasks/${id}/approve`);
  refreshTasks();
  return toTask(dto);
}

/** Read a natural-language phrase into a schedule and its next fire times.
 *  Nothing is saved. */
export async function parseSchedule(
  phrase: string,
  timezone: string,
): Promise<ParsedSchedule> {
  return api.post<ParsedSchedule>("/tasks/parse-schedule", {
    phrase,
    timezone,
  });
}

/** The backend's message for a failure, verbatim; `fallback` covers a
 *  transport failure that produced none. */
export function taskErrorMessage(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}
