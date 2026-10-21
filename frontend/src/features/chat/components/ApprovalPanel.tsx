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

/** How tall the opened call may grow before it scrolls inside itself. `formatArgs` does
 *  not truncate, and a call carrying a file's whole contents would otherwise push the
 *  buttons off the bottom of the dock — the same cap, for the same reason, that
 *  `ToolCallCard` puts on its own argument block. */
const ARGS_MAX_HEIGHT = "max-h-40";

/**
 * **The operator's decision point for sensitive actions the agent paused on.**
 *
 * It reports decisions upward rather than submitting them, because it is no longer the
 * whole of what a park can be waiting for: the same park may also hold questions, and the
 * run resumes on one body covering all of it. `ParkDock` owns the submit; this owns the
 * approve/deny state and the grant opt-in.
 *
 * Each approval also offers an opt-in "allow for the rest of this conversation" grant
 * (off by default): when checked and approved, the backend records a grant so the same
 * act auto-approves for the rest of the conversation instead of re-prompting. The grant's
 * scope is the backend's to derive from the parked call — for a tool that runs a command
 * it is that command, not the tool — and this only asks for one.
 *
 * ── What the card shows, and what it keeps back ──
 * A name, the tool's own plain-language explanation, and **the call itself behind a
 * toggle**. The raw rendering used to be the headline, which for a submitted plan meant
 * the entire document on one line above the buttons — a wall of text nobody reads, in the
 * place the operator most needs to be reading. It is still one click away, because a
 * shell command the operator cannot see is a decision they cannot actually make; it is
 * simply no longer the first thing on the card.
 *
 * ── The plan, and why it is a branch rather than a component ──
 * A submitted plan is an approval like any other — same park, same resume, same single
 * body — that happens to be read as a document and answered with three words instead of
 * two. Everything that differs is presentation: what sits behind the toggle, whether a
 * standing grant is even a coherent thing to offer, and whether "revise" is on the table.
 * All three are read off the call's own tool name, so no caller has to remember to say so
 * and no caller can disagree with another about it.
 */
export function ApprovalPanel(props: {
  approvals: Approval[];
  /** Collected upward on every change; complete only once every call is decided.
   *
   *  A `revise` decision arrives here with **no message**: the note is composed in the
   *  dock's own composer and attached on send, because a textarea inside a card is a
   *  second input a few pixels from the one the operator already types in. */
  onChange: (decisions: ApprovalDecision[], allDecided: boolean) => void;
  /** Put the Plan panel on screen. Offered beside a submitted plan, which is the one
   *  approval whose subject is too long to live on the card. Omit it where there is no
   *  panel to open — a compare pane has no viewport — and the button is not shown. */
  onReadPlan?: () => void;
  /** The frame around the panel already names the one call it holds (the dock's band
   *  reads `Approval needed · <tool>`), so the card does not repeat it. */
  named?: boolean;
  /** Set when this panel holds the whole of what a submit would send, and it holds one
   *  call: a verdict then settles it on the spot, with the grant tick as it stood, and
   *  there is no submit button to press after. Not called for a request for changes —
   *  that answer still needs its note. */
  onSettle?: (decisions: ApprovalDecision[]) => void | Promise<void>;
  /** Bind Y / N to Approve / Deny while the lone call is unanswered. Left off where a
   *  second panel may be on screen (a sub-agent's), since one key cannot answer two. */
  keys?: boolean;
}): JSX.Element {
  const [answers, setAnswers] = createSignal<Record<string, Answer>>({});
  const grant = createGrantToggle();

  /** A submitted plan: revisable, ungrantable, and read as a document rather than as a
   *  rendering of its own arguments. */
  const isPlan = (approval: Approval) => approval.name === PLAN_SUBMIT_TOOL;

  // The command an approval would run, when it runs one. The grant it can opt into is
  // scoped to that act rather than to the tool, so it keys the opt-in and names it.
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
      // Released when the submission ends, so a delivery that failed can be answered
      // again rather than leaving dead buttons.
      void Promise.resolve(props.onSettle(decisionsOf(next))).finally(() => {
        settled = false;
      });
    }
  };

  // The lone call's keys. Unmodified, so the keymap already ignores them while a field
  // has focus — the note being written for a revision is safe.
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

  /** The plan's own headline, for the card. Its title is the one line of a submitted
   *  plan that belongs on a decision card; the body is the Plan panel's to render. */
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
            // The approval pads its own body (design §10.18): the frame imposes none.
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
                  {/* The one thing a reviewer can say that changes how this decision should
                    be read. It used to refuse such a call outright, which kept the
                    operator out of the decision they most need to be in; now the call
                    arrives here, and the finding has to arrive with it. */}
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

              {/* A plan's subject is a document, so the card names it and points at the
                  panel that holds it. Everything else says what it is about to do in the
                  tool's own words. */}
              <Show
                when={isPlan(approval)}
                fallback={
                  /* The tool's own words where it has any — and the backend's one-line
                     rendering where it has none, which is most of them: only a handful
                     of tools take an `explanation` argument at all, so without the
                     fallback a `shell_run_command` card would name the tool and say
                     nothing whatever about what it is going to run. That is safe to show
                     here now that `summarize_call` elides at the source; a plan, the one
                     summary that was unreadable, never reaches this branch. */
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

              {/* The call itself — kept, and kept back. See the note at the top: the
                  operator can always see exactly what would run, but they are not made to
                  read it before they can reach the buttons.
                  `formatArgs`, not `approval.summary`: the summary is the backend's
                  one-line rendering and is elided to fit a notification, where the args
                  are the call as it was actually made. A toggle labelled "Show call" that
                  showed an abridged one would be the worst of both. */}
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

              {/* A plan is submitted once and answered once; there is no recurring act
                  for a standing grant to stand for. */}
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

              {/* The filled cell is the verdict given — Approve until one is. Luminance
                  says which answer is standing, the flag above says it in words.
                  "Request changes" only where trying again is meaningful: a refused
                  command is refused; a plan is a draft, and the answer to one is
                  usually neither yes nor no but "not like that". */}
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
