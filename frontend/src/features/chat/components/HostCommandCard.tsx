import { For, Show, createSignal, type JSX } from "solid-js";
import { ActionButtons, ActionFrame, Row, Stack, StatusFlag, Text } from "~/ui";
import { duration } from "~/lib/format";
import { grantKey, type GrantWidth } from "../commandScope";
import type { ApprovalDecision, HostCommand, HostCommandPhase } from "../model";
import { CautionsAndGrant } from "./ApprovalCautions";
import { CommandBoundary, FenceNote } from "./CommandBoundary";
import { createGrantToggle } from "./ConversationGrantToggle";
import { createAdoptedOpen } from "./ProcessRow";

/** The state at the plate's far end. Hue only for running (info) and failed or denied
 *  (alert); a finished command is dim, since a green OK on every success spends the accent on nothing. */
const phaseFlag: Record<
  HostCommandPhase,
  { tone: "warn" | "info" | "dim" | "alert"; label: string }
> = {
  pending: { tone: "warn", label: "Awaiting approval" },
  running: { tone: "info", label: "Running" },
  ok: { tone: "dim", label: "Done" },
  error: { tone: "alert", label: "Failed" },
  denied: { tone: "alert", label: "Denied" },
  stale: { tone: "dim", label: "Decided elsewhere" },
};

/**
 * Commands rendered as persistent terminals: the exact command, gated behind
 * APPROVE/DENY, then the same block shows stdout/stderr/exit once it has run.
 *
 * More than one tool lands here (the sandboxed host command and the worktree shell), so
 * each command carries its own `name`: a conversation grant is recorded against the tool
 * and the command it ran, and keying on the tool alone would make one tick a standing yes
 * to everything the thread tries.
 *
 * The backend resumes the parked run only on a decision covering every pending command,
 * so decisions are submitted as one batch once all are answered.
 */
export function HostCommandCard(props: {
  commands: HostCommand[];
  onSubmit: (decisions: ApprovalDecision[]) => void | Promise<void>;
  /** Controls the runtime-output collapse (expand-all/collapse-all). Defaults to
   *  expanded; the command line and decision controls always stay visible. */
  open?: boolean;
}): JSX.Element {
  // Grant scope is captured when the command is decided, not at batch submit, so a later
  // checkbox toggle can't change an already-locked choice.
  type Decision = { approved: boolean; scope: ApprovalDecision["scope"] };
  const [decisions, setDecisions] = createSignal<Record<string, Decision>>({});
  const grant = createGrantToggle();
  const [submitting, setSubmitting] = createSignal(false);

  const pending = () => props.commands.filter((c) => c.phase === "pending");
  const isComplete = (d: Record<string, Decision>) =>
    pending().every((c) => c.toolCallId in d);
  const allDecided = () => isComplete(decisions());

  // The backend scopes a grant to the command, not the tool.
  const keyOf = (command: HostCommand) =>
    grantKey(command.name, command.command, command.toolCallId);

  async function decide(toolCallId: string, approved: boolean): Promise<void> {
    if (submitting()) return;
    const asked = props.commands.find((c) => c.toolCallId === toolCallId);
    const next = {
      ...decisions(),
      [toolCallId]: {
        approved,
        scope: asked ? grant.scope(keyOf(asked), approved) : ("once" as const),
      },
    };
    setDecisions(next);
    // The resume requires decisions covering exactly the pending calls.
    if (!isComplete(next)) return;
    setSubmitting(true);
    const payload: ApprovalDecision[] = Object.entries(next).map(
      ([tool_call_id, d]) => ({
        tool_call_id,
        approved: d.approved,
        scope: d.scope,
      }),
    );
    try {
      await props.onSubmit(payload);
      // A command still pending after the submit never landed (a 403 for an API
      // token, a dropped connection): clear the verdicts so the buttons come back.
      if (pending().length > 0) setDecisions({});
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Stack gap={2}>
      <For each={props.commands}>
        {(command) => (
          <Terminal
            command={command}
            decided={decisions()[command.toolCallId]?.approved}
            held={command.toolCallId in decisions() && !allDecided()}
            submitting={submitting()}
            open={props.open}
            sessionWidth={grant.widthOf(keyOf(command))}
            onToggleSession={(v) => grant.set(keyOf(command), v)}
            onDecide={decide}
          />
        )}
      </For>
    </Stack>
  );
}

/** A single host command as a terminal: command line, decision (while pending),
 *  then runtime output once it has run. */
function Terminal(props: {
  command: HostCommand;
  decided?: boolean;
  /** Decided locally, but the batch is held until sibling commands are decided. */
  held?: boolean;
  submitting?: boolean;
  /** When defined, controls the runtime-output collapse; defaults to expanded. */
  open?: boolean;
  /** How far approving this command also grants it for the rest of the conversation —
   *  nothing, this command, or everything the tool runs. */
  sessionWidth?: GrantWidth;
  onToggleSession?: (width: GrantWidth) => void;
  onDecide: (toolCallId: string, approved: boolean) => void;
}): JSX.Element {
  const c = () => props.command;
  // An unknown phase (newer backend, replayed event) falls back to a neutral flag.
  const flag = () =>
    phaseFlag[c().phase] ?? { tone: "dim" as const, label: c().phase };
  // Rests open, since its output is the point; yields to the turn's expand-all/collapse-all.
  const { open: showOutput } = createAdoptedOpen(props, true);
  // Decided locally, before the optimistic transition off "pending".
  const decidedPending = () =>
    c().phase === "pending" && props.decided !== undefined;
  // `timedOut` is listed because a timeout has a null exit code and a killed silent command still has that to say.
  const hasOutput = () =>
    Boolean(c().stdout) ||
    Boolean(c().stderr) ||
    c().error != null ||
    c().fenceNote != null ||
    c().timedOut === true ||
    c().exitCode != null;

  const plate = () => (c().reach ? `Host · ${c().reach}` : "Host");

  /** The backend's measurement around the spawn (a local stopwatch would time the stream);
   *  while running it comes from the progress ticks. */
  const elapsed = () =>
    c().elapsedMs !== undefined ? duration(c().elapsedMs!) : undefined;

  const body = (): JSX.Element => (
    <Stack gap={2} class="px-3 py-2">
      <Row gap={2} align="start">
        <Text variant="code" tone="dim" class="shrink-0">
          $
        </Text>
        <Text
          variant="code"
          tone="bright"
          class="min-w-0 whitespace-pre-wrap break-all"
        >
          {c().command}
        </Text>
      </Row>

      <Show when={c().phase === "pending"}>
        <Show when={c().explanation}>
          <Text variant="body" tone="default" class="break-words">
            {c().explanation}
          </Text>
        </Show>
        {/* The Auto review hands a `too_destructive` command to the operator, so the
            finding must arrive with the command rather than on a collapsed row above. */}
        <Show when={c().risk === "too_destructive"}>
          <Row gap={2} align="center">
            <StatusFlag status="alert">Cannot be undone</StatusFlag>
            <Text variant="micro" tone="warn" class="break-words">
              {c().reviewReason}
            </Text>
          </Row>
        </Show>
        <Show
          when={!decidedPending()}
          fallback={
            <Text variant="micro" tone="dim">
              {props.decided ? "Approved" : "Denied"} —{" "}
              {props.held ? "awaiting the other decisions…" : "submitting…"}
            </Text>
          }
        >
          <Stack gap={2}>
            {/* Above the decision because its state is captured when APPROVE is clicked
                (see `decide`). A guarded call offers none. */}
            <CautionsAndGrant
              cautions={c()}
              command={c().command}
              toolName={c().name}
              width={props.sessionWidth}
              disabled={props.submitting}
              onChange={(v) => props.onToggleSession?.(v)}
            />
            <ActionButtons
              buttons={[
                {
                  label: "Approve & run",
                  primary: true,
                  disabled: props.submitting,
                  onClick: () => props.onDecide(c().toolCallId, true),
                },
                {
                  label: "Deny",
                  disabled: props.submitting,
                  onClick: () => props.onDecide(c().toolCallId, false),
                },
              ]}
            />
          </Stack>
        </Show>
      </Show>

      {/* Shown from the moment the declaration exists, before the command runs. */}
      <CommandBoundary command={c()} />

      {/* Live output, streamed in deltas. One stream here, two once settled: mid-flight
            the wire concatenates stdout and stderr with no field to tell them apart, and
            guessing would file half a stack trace under the wrong heading. The result
            separates them, so this arm disappears when the call completes. */}
      <Show when={c().phase === "running"}>
        <Show
          when={c().streamed}
          fallback={
            <Text variant="micro" tone="info">
              Running…
            </Text>
          }
        >
          <Text
            as="div"
            variant="code"
            tone="default"
            class="max-h-64 overflow-y-auto whitespace-pre-wrap break-all"
          >
            {c().streamed}
          </Text>
        </Show>
      </Show>

      <Show when={c().phase === "denied"}>
        <Text variant="body" tone="alert">
          Denied — not executed.
        </Text>
      </Show>

      {/* The decision 409'd: resolved elsewhere (another tab, a retry). Non-interactive;
            a refetch reconciles the transcript. */}
      <Show when={c().phase === "stale"}>
        <Text variant="body" tone="dim">
          Decided elsewhere — this was resolved from another session; the
          transcript will catch up shortly.
        </Text>
      </Show>

      <Show when={hasOutput() && showOutput()}>
        <Stack gap={1}>
          <Show when={c().stdout}>
            <Text
              as="div"
              variant="code"
              tone="default"
              class="whitespace-pre-wrap break-all"
            >
              {c().stdout}
            </Text>
          </Show>
          <Show when={c().stderr}>
            <Text
              as="div"
              variant="code"
              tone="alert"
              class="whitespace-pre-wrap break-all"
            >
              {c().stderr}
            </Text>
          </Show>
          <Show when={c().error}>
            <Text variant="micro" tone="warn" class="break-words">
              {c().error}
            </Text>
          </Show>
          {/* Beneath the streams because it explains them. */}
          <FenceNote command={c()} />
          {/* A timeout never exited, so its marker stands without an exit code. */}
          <Show when={c().timedOut || c().exitCode != null}>
            <Text variant="meta" tone={c().exitCode === 0 ? "dim" : "alert"}>
              {c().timedOut ? "Timed out" : `Exit ${c().exitCode}`}
            </Text>
          </Show>
        </Stack>
      </Show>
    </Stack>
  );

  return (
    <Show
      when={c().phase === "pending"}
      fallback={
        // A ruled frame named by its plate: its edge does work because it holds a command
        // and what came back from the operator's machine.
        <section class="flex flex-col border border-line-strong">
          <div class="flex items-center gap-2 border-b border-line px-3 py-2">
            <Text variant="plate" tone="dim" class="min-w-0 flex-1 truncate">
              {plate()}
            </Text>
            <Show when={elapsed()}>
              {(t) => (
                <Text variant="meta" tone="dim" class="tabular-nums">
                  {t()}
                </Text>
              )}
            </Show>
            <Text variant="meta" tone={flag().tone}>
              {flag().label}
            </Text>
          </div>
          {body()}
        </section>
      }
    >
      {/* Waiting on the operator: the action tier. */}
      <ActionFrame
        tone="approval"
        title={`Approval needed · ${plate()}`}
        aside={elapsed()}
      >
        {body()}
      </ActionFrame>
    </Show>
  );
}
