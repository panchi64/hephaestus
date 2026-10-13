import { createSignal, For, Show, type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import {
  Button,
  ExpandableText,
  Icon,
  Modal,
  Row,
  Stack,
  StatusFlag,
  Text,
  type Status,
} from "~/ui";
import { relativeTime, timestamp } from "~/lib/format";
import { openConversation } from "~/features/chat/data";
import { useTaskRuns } from "../data";
import type { TaskRun, TaskRunOutcome } from "../model";

const OUTCOME_STATUS: Record<TaskRunOutcome, Status> = {
  ok: "nominal",
  error: "alert",
  blocked: "warn",
  cancelled: "idle",
  skipped: "info",
};

const OUTCOME_ICON: Record<
  TaskRunOutcome,
  "check" | "warning" | "lock" | "close" | "clock"
> = {
  ok: "check",
  error: "warning",
  blocked: "lock",
  cancelled: "close",
  skipped: "clock",
};

/** `TaskRun.outcome` is null between the scheduler's started-row insert and
 *  its finalize (a still-live execution) — these give the still-running row
 *  its own render arm instead of indexing the outcome maps with null. */
function runStatus(run: TaskRun): Status {
  return run.outcome === null ? "live" : OUTCOME_STATUS[run.outcome];
}

function runIcon(
  run: TaskRun,
): "check" | "warning" | "lock" | "close" | "clock" | "activity" {
  return run.outcome === null ? "activity" : OUTCOME_ICON[run.outcome];
}

function runLabel(run: TaskRun): string {
  return run.outcome === null ? "Running" : run.outcome.toUpperCase();
}

const RUNS_PAGE = 6;

/** A routine's expanded run history — outcome chip, timestamps, and a deep-link
 *  to the conversation the run drove (agent routines only). Fetches lazily via
 *  `useTaskRuns`, mounted only while the routine row is expanded. */
export function TaskRunHistory(props: { taskId: string }): JSX.Element {
  const runsResource = useTaskRuns(() => props.taskId);
  const navigate = useNavigate();
  const [limit, setLimit] = createSignal(RUNS_PAGE);
  const [outputRun, setOutputRun] = createSignal<TaskRun | null>(null);

  const runs = () => runsResource() ?? [];
  const shown = () => runs().slice(0, limit());
  const hidden = () => Math.max(runs().length - limit(), 0);

  function openRunConversation(run: TaskRun) {
    if (!run.conversationId) return;
    openConversation(run.conversationId);
    navigate("/chat");
  }

  return (
    <Stack gap={2}>
      <Row gap={2} align="baseline">
        <Text variant="label" tone="dim">
          Run history
        </Text>
        <Text variant="micro" tone="dim">
          {runs().length}
        </Text>
      </Row>
      <Show
        when={runs().length}
        fallback={
          <Text variant="body" tone="dim">
            No runs yet.
          </Text>
        }
      >
        <Stack gap={0}>
          <For each={shown()}>
            {(run) => (
              <div class="flex flex-col gap-1 py-2">
                <Row gap={2} align="center">
                  <Icon
                    name={runIcon(run)}
                    size={12}
                    class="shrink-0 text-dim"
                  />
                  <span class="flex-1" />
                  <StatusFlag status={runStatus(run)}>
                    {runLabel(run)}
                  </StatusFlag>
                  <Text variant="micro" tone="dim">
                    {relativeTime(run.startedAt)}
                  </Text>
                  <Show when={run.conversationId}>
                    <Button
                      variant="ghost"
                      size="sm"
                      leading="chat"
                      onClick={(e) => {
                        e.stopPropagation();
                        openRunConversation(run);
                      }}
                    >
                      View chat
                    </Button>
                  </Show>
                </Row>
                <Show
                  when={run.summary}
                  fallback={
                    <Text variant="micro" tone="dim">
                      No summary
                    </Text>
                  }
                >
                  <ExpandableText
                    text={run.summary!}
                    limit={120}
                    variant="micro"
                    tone="dim"
                  />
                  <Show when={run.summary!.length > 120}>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setOutputRun(run)}
                      class="self-start"
                    >
                      View output
                    </Button>
                  </Show>
                </Show>
              </div>
            )}
          </For>
          <Show when={hidden() > 0}>
            <Button
              variant="ghost"
              size="sm"
              leading="chevron-down"
              onClick={() => setLimit((n) => n + RUNS_PAGE)}
              class="mt-1 self-start"
            >
              SHOW MORE ({hidden()})
            </Button>
          </Show>
        </Stack>
      </Show>

      <Modal
        open={outputRun() !== null}
        onClose={() => setOutputRun(null)}
        title="Run output"
        class="max-w-2xl"
        footer={
          <Button variant="ghost" onClick={() => setOutputRun(null)}>
            Close
          </Button>
        }
      >
        <Show when={outputRun()}>
          {(run) => (
            <Stack gap={3}>
              <Row gap={2} align="center">
                <StatusFlag status={runStatus(run())}>
                  {runLabel(run())}
                </StatusFlag>
                <Text variant="micro" tone="dim">
                  {timestamp(run().startedAt)}
                </Text>
              </Row>
              <div
                class="overflow-auto bg-raised p-3 font-mono"
                style={{ "max-height": "60vh" }}
              >
                <Text variant="body" tone="dim" class="whitespace-pre-wrap">
                  {run().summary ?? "No summary"}
                </Text>
              </div>
            </Stack>
          )}
        </Show>
      </Modal>
    </Stack>
  );
}
