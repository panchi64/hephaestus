import { For, Show, createSignal, type JSX } from "solid-js";
import { ActionButtons, ActionFrame, Row, Stack, StatusFlag, Text } from "~/ui";
import { duration } from "~/lib/format";
import { grantKey, type GrantWidth } from "../commandScope";
import type { ApprovalDecision, HostCommand, HostCommandPhase } from "../model";
import { offersGrant } from "../approvalCautions";
import { ApprovalCautions } from "./ApprovalCautions";
import { CommandBoundary, FenceNote } from "./CommandBoundary";
import {
  ConversationGrantToggle,
  createGrantToggle,
} from "./ConversationGrantToggle";
import { createAdoptedOpen } from "./ProcessRow";

/** The state at the plate's far end, in mono. Hue only where it means something —
 *  running (info), failed or denied (alert); a command that finished is dim, because a
 *  green OK on every successful command is the one accent spent on nothing. */
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
 * Commands rendered as persistent terminals. Each block shows the exact command the
 * agent wants to run on the operator's machine, gates it behind APPROVE/DENY, and —
 * once approved — keeps the same block to show the runtime output (stdout/stderr/exit).
 * The block never collapses into a separate tool card, so the operator keeps one
 * continuous readout of what ran.
 *
 * **More than one tool lands here**, which is why each command carries its own `name`:
 * the sandboxed host command and the worktree shell both run something the operator has
 * to see, and a conversation grant is recorded against a tool *and the command it ran*.
 * Keying the grant off one literal would have quietly recorded the wrong tool for the
 * other; keying it off the tool alone would make one tick under a test run a standing yes
 * to every command the thread goes on to try.
 *
 * The backend resumes the parked run only on a decision covering *every* pending
 * command, so decisions are collected and submitted as a single batch once all
 * pending commands are answered.
 */
export function HostCommandCard(props: {
  commands: HostCommand[];
  onSubmit: (decisions: ApprovalDecision[]) => void | Promise<void>;
  /** Controls the runtime-output collapse (expand-all/collapse-all). Defaults to
   *  expanded; the command line and decision controls always stay visible. */
  open?: boolean;
}): JSX.Element {
  // Each decision captures its grant scope at the instant that command is decided,
  // not at batch-submit — so toggling the checkbox after a command is approved can't
  // retroactively change (or lose) that command's already-locked grant choice.
  type Decision = { approved: boolean; scope: ApprovalDecision["scope"] };
  const [decisions, setDecisions] = createSignal<Record<string, Decision>>({});
  const grant = createGrantToggle();
  const [submitting, setSubmitting] = createSignal(false);

  const pending = () => props.commands.filter((c) => c.phase === "pending");
  const isComplete = (d: Record<string, Decision>) =>
    pending().every((c) => c.toolCallId in d);
  const allDecided = () => isComplete(decisions());

  // The act a grant on one of these would name — the command, not the tool, since the
  // backend scopes the grant to the command it was recorded from.
  const keyOf = (command: HostCommand) =>
    grantKey(command.name, command.command, command.toolCallId);

  async function decide(toolCallId: string, approved: boolean): Promise<void> {
    if (submitting()) return;
    // Lock in this command's grant choice now, keyed by the act that actually asked,
    // rather than re-reading the checkbox at submit.
    const asked = props.commands.find((c) => c.toolCallId === toolCallId);
    const next = {
      ...decisions(),
      [toolCallId]: {
        approved,
        scope: asked ? grant.scope(keyOf(asked), approved) : ("once" as const),
      },
    };
    setDecisions(next);
    // Hold until every pending command is answered, then submit them together —
    // the resume requires decisions covering exactly the pending calls.
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
  // A phase the map doesn't know (a newer backend, a replayed event) must not
  // throw on the property read below — fall back to a neutral flag.
  const flag = () =>
    phaseFlag[c().phase] ?? { tone: "dim" as const, label: c().phase };
  // The terminal rests open — its output is the point of it — and yields to the
  // turn's expand-all/collapse-all like every other card.
  const { open: showOutput } = createAdoptedOpen(props, true);
  // Decided locally, before the optimistic transition off "pending".
  const decidedPending = () =>
    c().phase === "pending" && props.decided !== undefined;
  // `timedOut` is in the list because a timeout's exit code is null — it never exited —
  // and a killed command that printed nothing still has the one thing worth saying.
  const hasOutput = () =>
    Boolean(c().stdout) ||
    Boolean(c().stderr) ||
    c().error != null ||
    c().fenceNote != null ||
    c().timedOut === true ||
    c().exitCode != null;

  /** The region's name: where the command runs, and how far it said it reaches. */
  const plate = () => (c().reach ? `Host · ${c().reach}` : "Host");

  /** The backend's own measurement, taken around the spawn — never a stopwatch started
   *  here, which would be timing the stream rather than the command. While it runs the
   *  figure comes from the progress ticks, so a long command counts up instead of
   *  sitting at nothing until it ends. */
  const elapsed = () =>
    c().elapsedMs !== undefined ? duration(c().elapsedMs!) : undefined;

  const body = (): JSX.Element => (
    <Stack gap={2} class="px-3 py-2">
      {/* The exact command line the agent asked to run on the host — mono, the
          machine's voice, with the prompt receding so the command is what reads. */}
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

      {/* Pending: the operator's judgment aid, then the decision. */}
      <Show when={c().phase === "pending"}>
        <Show when={c().explanation}>
          <Text variant="body" tone="default" class="break-words">
            {c().explanation}
          </Text>
        </Show>
        {/* What the Auto review found, when one ran. `too_destructive` is the whole
              reason it is here: the review used to refuse such a command itself, and now
              it hands it over — so the finding has to arrive with the command rather than
              on a collapsed row above it. */}
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
            <ApprovalCautions cautions={c()} />
            {/* The grant opt-in sits *above* the decision so it reads naturally; its
                  state is captured into this command's decision the moment APPROVE is
                  clicked (see `decide`), so set it before approving. A guarded call
                  offers none. */}
            <Show when={offersGrant(c())}>
              <ConversationGrantToggle
                command={c().command}
                toolName={c().name}
                width={props.sessionWidth}
                disabled={props.submitting}
                onChange={(v) => props.onToggleSession?.(v)}
              />
            </Show>
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

      {/* What it declared and what held it to that — on the card from the moment the
            declaration exists, which is before it runs as well as after. */}
      <CommandBoundary command={c()} />

      {/* Approved and executing — text readout, never a spinner, and now the output
            itself as it arrives. The run streams it in deltas every half-second, so a
            command that takes a minute reads as a command working rather than as a
            stalled turn.

            **One stream here where the settled command has two.** Mid-flight the wire
            carries stdout and stderr concatenated with no field to tell them apart, and
            splitting them on a guess would file half a stack trace under the wrong
            heading. The result is the record and separates them properly, which is why
            this arm disappears the moment the call completes. */}
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

      {/* Denied — it never ran. */}
      <Show when={c().phase === "denied"}>
        <Text variant="body" tone="alert">
          Denied — not executed.
        </Text>
      </Show>

      {/* This decision 409'd — already resolved elsewhere (a second tab, a
            retried request) by the time it landed. Non-interactive; the
            transcript reconciles with the actual outcome via a refetch. */}
      <Show when={c().phase === "stale"}>
        <Text variant="body" tone="dim">
          Decided elsewhere — this was resolved from another session; the
          transcript will catch up shortly.
        </Text>
      </Show>

      {/* Runtime output, once the command has run. */}
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
          {/* Beneath the streams, because it is about them: a permission error up
                there is the fence holding, not the command breaking. */}
          <FenceNote command={c()} />
          {/* A timeout has no exit code — it never exited — so the marker has to be
                able to stand without one, and the null is the fact rather than a
                missing value. */}
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
        // A ruled frame named by its plate (design §7, §10.14): the one region in a
        // turn whose edge does work, since it holds a command and what came back from
        // the operator's own machine. Contents bare; the state reports in mono at the
        // plate's far end and spends a colour only where something went wrong or is
        // still going — never a green OK.
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
      {/* Waiting on the operator: the action tier (design §10.18). */}
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
