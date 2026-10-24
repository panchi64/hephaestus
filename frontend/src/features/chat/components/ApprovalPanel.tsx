import { createSignal, For, Show, type JSX } from "solid-js";
import {
  ActionButtons,
  Button,
  cx,
  Disclosure,
  Row,
  Stack,
  StatusFlag,
  Text,
} from "~/ui";
import {
  decisionsFor,
  ONE_CLICK_KEYS,
  settlesImmediately,
  type Answer,
} from "../approvalDecisions";
import { grantKey } from "../commandScope";
import { registerKeymap } from "~/lib/keymap";
import { formatArgs } from "../data";
import type { Approval, ApprovalDecision } from "../model";
import { PLAN_SUBMIT_TOOL } from "../stream/approvals";
import { CautionsAndGrant } from "./ApprovalCautions";
import { createGrantToggle } from "./ConversationGrantToggle";

/** Caps the opened call's height; `formatArgs` doesn't truncate, so a whole file's contents
 *  would push the buttons off the dock. Same cap as `ToolCallCard`'s argument block. */
const ARGS_MAX_HEIGHT = "max-h-40";

/**
 * The operator's decision point for sensitive actions the agent paused on.
 *
 * Reports decisions upward rather than submitting: a park may also hold questions and the
 * run resumes on one body covering all of it. `ParkDock` owns the submit; this owns the
 * approve/deny state and the grant opt-in.
 *
 * Each approval offers an opt-in (off by default) "allow for the rest of this
 * conversation" grant; the backend derives its scope from the parked call.
 *
 * The call itself sits behind a toggle: a plan's whole document must not be the
 * headline, yet a command the operator cannot see is a decision they cannot make.
 *
 * A submitted plan is a branch, not a component: same park and resume, differing only in
 * presentation (toggle content, no standing grant, "revise" on offer), all read off the
 * call's tool name.
 */
export function ApprovalPanel(props: {
  approvals: Approval[];
  /** Collected upward on every change; complete only once every call is decided.
   *  A `revise` decision arrives with no message: the note is composed in the dock's
   *  composer and attached on send. */
  onChange: (decisions: ApprovalDecision[], allDecided: boolean) => void;
  /** Opens the Plan panel, offered beside a submitted plan. Omit where there is no
   *  panel (a compare pane has no viewport) and the button is hidden. */
  onReadPlan?: () => void;
  /** The frame already names the one call it holds (the dock band reads
   *  `Approval needed · <tool>`), so the card does not repeat it. */
  named?: boolean;
  /** Set when this panel holds everything a submit would send and exactly one call: a
   *  verdict settles it on the spot, with the grant tick as it stood. Not called for a
   *  request for changes, which still needs its note. */
  onSettle?: (decisions: ApprovalDecision[]) => void | Promise<void>;
  /** Bind Y / N to Approve / Deny while the lone call is unanswered. Off where a second
   *  panel may be on screen (a sub-agent's); one key cannot answer two. */
  keys?: boolean;
}): JSX.Element {
  const [answers, setAnswers] = createSignal<Record<string, Answer>>({});
  const grant = createGrantToggle();

  const isPlan = (approval: Approval) => approval.name === PLAN_SUBMIT_TOOL;

  // The grant is scoped to the command, not the tool, so it keys the opt-in.
  const commandOf = (approval: Approval): string | undefined =>
    typeof approval.args.command === "string"
      ? approval.args.command
      : undefined;
  const keyOf = (approval: Approval) =>
    grantKey(approval.name, commandOf(approval), approval.toolCallId);

  const decisionsOf = (given: Record<string, Answer>) =>
    decisionsFor(props.approvals, given, (a, approved) =>
      grant.scope(keyOf(a), approved),
    );

  const emit = () => {
    const given = answers();
    props.onChange(
      decisionsOf(given),
      props.approvals.every((a) => a.toolCallId in given),
    );
  };

  let settled = false;
  const decide = (
    toolCallId: string,
    approved: boolean,
    intent: "deny" | "revise" = "deny",
  ) => {
    if (settled) return;
    const answer: Answer = { approved, intent };
    const next = { ...answers(), [toolCallId]: answer };
    setAnswers(next);
    emit();
    if (props.onSettle && settlesImmediately(answer)) {
      settled = true;
      // Released when the submission ends so a failed delivery can be answered again.
      void Promise.resolve(props.onSettle(decisionsOf(next))).finally(() => {
        settled = false;
      });
    }
  };

  // Unmodified keys, which the keymap ignores while a field has focus.
  registerKeymap(() => {
    const only = props.approvals[0];
    if (!props.keys || !props.onSettle || props.approvals.length !== 1 || !only)
      return [];
    return [
      {
        combo: ONE_CLICK_KEYS.approve,
        run: () => decide(only.toolCallId, true),
      },
      {
        combo: ONE_CLICK_KEYS.deny,
        run: () => decide(only.toolCallId, false, "deny"),
      },
    ];
  });

  /** The title is the one line of a plan that belongs on the card; the Plan panel renders the body. */
  const planTitle = (approval: Approval): string =>
    typeof approval.args.title === "string" ? approval.args.title : "Untitled";
  const planSteps = (approval: Approval): number =>
    Array.isArray(approval.args.steps) ? approval.args.steps.length : 0;

  return (
    <div class="flex flex-col divide-y divide-line">
      <For each={props.approvals}>
        {(approval) => {
          const answer = () => answers()[approval.toolCallId];
          const verdict = () => {
            const given = answer();
            if (!given) return null;
            if (given.approved) return { tone: "nominal", label: "Approved" };
            return given.intent === "revise"
              ? { tone: "warn", label: "Changes requested" }
              : { tone: "alert", label: "Denied" };
          };
          return (
            // The approval pads its own body; the frame imposes none.
            <Stack gap={3} class="px-3 py-3">
              <Show
                when={
                  !props.named ||
                  approval.risk === "too_destructive" ||
                  verdict()
                }
              >
                <Row gap={2} align="center" class="flex-wrap">
                  <Show when={!props.named}>
                    <Text
                      variant="meta"
                      tone="bright"
                      class="min-w-0 break-all"
                    >
                      {approval.name}
                    </Text>
                  </Show>
                  {/* The reviewer's finding travels with the call; the operator decides, it isn't refused outright. */}
                  <Show when={approval.risk === "too_destructive"}>
                    <StatusFlag status="alert">Cannot be undone</StatusFlag>
                  </Show>
                  <Show when={verdict()} keyed>
                    {(v) => (
                      <StatusFlag
                        status={v.tone as "nominal" | "alert" | "warn"}
                      >
                        {v.label}
                      </StatusFlag>
                    )}
                  </Show>
                </Row>
              </Show>

              {/* A plan is a document: the card names it and points at its panel. */}
              <Show
                when={isPlan(approval)}
                fallback={
                  /* The backend's one-line summary stands in where the tool has no
                     `explanation` argument (most tools), or a `shell_run_command` card would
                     say nothing about what it runs. Safe because `summarize_call` elides at
                     the source, and a plan never reaches this branch. */
                  <Show when={approval.explanation || approval.summary}>
                    {(line) => (
                      <Text variant="body" tone="bright">
                        {line()}
                      </Text>
                    )}
                  </Show>
                }
              >
                <Text variant="body" tone="bright">
                  {planTitle(approval)}
                </Text>
                <Row gap={2} align="center">
                  <Text variant="meta" tone="dim">
                    {planSteps(approval) === 1
                      ? "1 step"
                      : `${planSteps(approval)} steps`}
                  </Text>
                  <Show when={props.onReadPlan}>
                    <Button
                      variant="ghost"
                      size="sm"
                      leading="note"
                      onClick={() => props.onReadPlan?.()}
                    >
                      Read the plan
                    </Button>
                  </Show>
                </Row>
              </Show>

              <Show when={approval.risk === "too_destructive"}>
                <Text variant="body" tone="warn" class="break-words">
                  {approval.reviewReason}
                </Text>
              </Show>

              {/* `formatArgs`, not `approval.summary`: the summary is elided to fit a
                  notification, and "Show call" must show the call as made. */}
              <Show when={Object.keys(approval.args).length > 0}>
                <Disclosure label="Show call">
                  <div
                    class={cx(
                      "overflow-y-auto border border-line bg-bg px-3 py-2",
                      ARGS_MAX_HEIGHT,
                    )}
                  >
                    <Text
                      as="div"
                      variant="code"
                      tone="bright"
                      class="whitespace-pre-wrap break-words"
                    >
                      {formatArgs(approval.args)}
                    </Text>
                  </div>
                </Disclosure>
              </Show>

              {/* A plan is answered once; there is no recurring act for a grant to cover. */}
              <CautionsAndGrant
                cautions={approval}
                grantable={!isPlan(approval)}
                command={commandOf(approval)}
                toolName={approval.name}
                width={grant.widthOf(keyOf(approval))}
                onChange={(v) => {
                  grant.set(keyOf(approval), v);
                  emit();
                }}
              />

              {/* The filled cell is the verdict given (Approve until one is). "Request
                  changes" is offered only for a plan, a draft; a refused command is refused. */}
              <ActionButtons
                buttons={[
                  {
                    label: "Approve",
                    kbd: props.keys && props.onSettle ? "Y" : undefined,
                    primary: !answer() || answer()!.approved,
                    onClick: () => decide(approval.toolCallId, true),
                  },
                  ...(isPlan(approval)
                    ? [
                        {
                          label: "Request changes",
                          primary:
                            answer()?.approved === false &&
                            answer()?.intent === "revise",
                          onClick: () =>
                            decide(approval.toolCallId, false, "revise"),
                        },
                      ]
                    : []),
                  {
                    label: isPlan(approval) ? "Reject" : "Deny",
                    kbd: props.keys && props.onSettle ? "N" : undefined,
                    primary:
                      answer()?.approved === false &&
                      answer()?.intent === "deny",
                    onClick: () => decide(approval.toolCallId, false, "deny"),
                  },
                ]}
              />
            </Stack>
          );
        }}
      </For>
    </div>
  );
}
