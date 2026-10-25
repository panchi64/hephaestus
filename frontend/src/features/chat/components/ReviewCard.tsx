import { useSearchParams } from "@solidjs/router";
import { Show, type JSX } from "solid-js";
import { Button, Collapse, StatusFlag, Text } from "~/ui";
import type { Review } from "../model";
import { toolPresentation } from "../toolPresentation";
import { ProcessRow, Sep, createAdoptedOpen } from "./ProcessRow";

/** The verdict in the operator's words, as the row's own second segment.
 *
 *  Words rather than only a tone, because this is the one row in a turn whose whole
 *  content is a decision somebody else made on the operator's behalf — and a decision
 *  carried by colour alone is a decision they can scroll past (§12). Undefined is a real
 *  live state: the row appears when the review starts, so a review that costs a model call
 *  reads as work in flight instead of as a stalled turn. */
const VERDICT: Record<NonNullable<Review["decision"]>, string> = {
  allow: "allowed without asking you",
  ask: "handed to you",
  // No review produces this any more — an act it judged unrecoverable is handed over
  // instead — but a thread reviewed before that changed still replays the word.
  block: "refused",
};

/** Which stage settled it, spelled out. The distinction is the operator's to act on: a
 *  call cleared structurally means the rule is too broad, and one cleared by the decision
 *  model means the model judged it — two different things to go and change. */
const STAGE: Record<NonNullable<Review["stage"]>, string> = {
  judge: "Settled by its structure, with no model call.",
  reviewer: "Settled by the decision model.",
};

/** Why a call that needed the decision model was handed over instead. */
const SETUP: Record<NonNullable<Review["setup"]>, string> = {
  unbound: "No decision model is set up, so Auto asked you.",
  unreachable: "The decision model did not answer, so Auto asked you.",
};

/** The decision model's probabilities, in the order its questions are asked. */
const SCORE_ORDER = [
  "risk",
  "irreversible",
  "outward",
  "requested",
  "refused",
  "mismatch",
] as const;

function scoreLine(scores: Record<string, number>): string {
  return SCORE_ORDER.filter((key) => key in scores)
    .map((key) => `${key} ${scores[key].toFixed(2)}`)
    .join(" · ");
}

/** The ground the structural stage cleared it on. Three different arguments, not three
 *  degrees of trust — the one granted to a classified read grants nothing to a command,
 *  which is precisely what an operator auditing the level needs to be able to see. There
 *  is no networked ground: a command that reaches out is always the reviewer's, whatever
 *  domains are allowed. */
const TIER: Record<NonNullable<Review["tier"]>, string> = {
  read: "The tool only observes, whatever it is asked for.",
  sandbox: "It runs inside this conversation's VM.",
  workspace: "It stays in this conversation's folders and names no address.",
};

/** Where the command runs, shown for every reviewed command, cleared or not. */
const REACH: Record<NonNullable<Review["reach"]>, string> = {
  offline: "Runs in this conversation's VM.",
  network: "Runs in this conversation's VM and reaches the allowed domains.",
  host: "Runs on your machine, inside the host fence.",
};

const RISK: Record<NonNullable<Review["risk"]>, string> = {
  low: "Low risk",
  high: "High risk",
  too_destructive: "Cannot be undone",
};

const AUTHORIZATION: Record<NonNullable<Review["authorization"]>, string> = {
  explicitly_no: "you refused it",
  neutral: "you neither asked for it nor refused it",
  explicitly_yes: "you asked for it",
};

/** One action the chassis ruled on in the operator's place, at the Auto permission level.
 *
 *  **It shares the log's anatomy and dashes its trace**, exactly as the injection row
 *  does and for the same reason: every solid trace is something the *model* did, and
 *  this is not. The model asked; we answered for the operator. A dashed elbow into a
 *  dashed octagon beside the call it judged is what says that at a glance, before a word
 *  of the row is read.
 *
 *  **A refusal is the one state that gets a flag.** A cleared call is followed by the call
 *  itself and a parked one by an approval card, so both are accounted for by the row after
 *  them. A refused call is followed by nothing at all — this row is the entire record of
 *  it, and it is the single thing in a turn an operator is most likely to disagree with.
 *
 *  `open` makes expand/collapse controlled (expand-all/collapse-all); at rest the row is
 *  closed, because the verdict is on the row and only the grounds are behind it. */
export function ReviewCard(props: {
  review: Review;
  open?: boolean;
}): JSX.Element {
  const { open, toggle } = createAdoptedOpen(props);
  const [, setParams] = useSearchParams();
  const judged = () => toolPresentation(props.review.name).label;
  const verdict = () =>
    props.review.decision ? VERDICT[props.review.decision] : "checking…";
  return (
    <div class="group/review">
      {/* No `hover:bg-raised`: that fill is for the model's calls, the work an operator
          opens to inspect, and this one deliberately has none — the same posture the
          injection row takes. */}
      <ProcessRow
        open={open()}
        onToggle={toggle}
        icon="review"
        iconClass={
          props.review.decision === "block" ? "text-alert" : "text-dim"
        }
        frame="octagon"
        dashed
        label="Review"
        title={`Review of ${props.review.name}`}
        trailing={
          <Show when={props.review.decision === "block"}>
            <StatusFlag status="alert">Refused</StatusFlag>
          </Show>
        }
      >
        <Sep />
        <Text variant="micro" tone="dim" class="min-w-0 shrink-0">
          {judged()}
        </Text>
        <Sep />
        <Text variant="micro" tone="dim" class="min-w-0 truncate">
          {verdict()}
        </Text>
      </ProcessRow>
      {/* Outside the collapse: the operator has to see why they were asked, and the fix is
          one setting away. `?settings=` is the settings dialog's own address. */}
      <Show when={props.review.setup}>
        {(setup) => (
          <div class="flex flex-wrap items-center gap-2 px-2 py-1">
            <Text variant="micro" tone="warn">
              {SETUP[setup()]}
            </Text>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                setParams({ settings: "models" }, { scroll: false })
              }
            >
              Set up a decision model
            </Button>
          </div>
        )}
      </Show>
      <Collapse open={open()}>
        <div class="flex flex-col gap-1 px-2 py-1.5">
          {/* What the action would do at its worst — the same sentence the reviewer was
              judging, so the operator and the model looked at one description. */}
          <Text variant="micro" tone="default" class="break-words">
            {props.review.summary}
          </Text>
          {/* The rest of what the reviewer read — a delegated task, the replacement text
              of a skill, a stated reason for opening a credential. The operator is being
              shown the material the decision was made on, not a paraphrase of it, so it
              keeps the line breaks the model wrote it with. */}
          <Show when={props.review.detail}>
            {(detail) => (
              <Text
                variant="micro"
                tone="dim"
                class="whitespace-pre-wrap break-words"
              >
                {detail()}
              </Text>
            )}
          </Show>
          <Show when={props.review.reach}>
            {(reach) => (
              <Text variant="micro" tone="dim">
                {REACH[reach()]}
              </Text>
            )}
          </Show>
          <Show when={props.review.reach === "offline" && props.review.network}>
            <Text variant="micro" tone="dim">
              It installs, clones or names an address, so it runs with the
              allowed domains anyway.
            </Text>
          </Show>
          <Show when={props.review.stage}>
            {(stage) => (
              <Text variant="micro" tone="dim">
                {STAGE[stage()]}
              </Text>
            )}
          </Show>
          <Show when={props.review.tier}>
            {(tier) => (
              <Text variant="micro" tone="dim">
                {TIER[tier()]}
              </Text>
            )}
          </Show>
          {/* Why an ordinary contained command still went to review: only a VM with no
              network clears one on its shape. Not said of the host tool, which is never
              cleared that way. */}
          <Show
            when={
              props.review.reach &&
              props.review.reach !== "host" &&
              props.review.fenced === false
            }
          >
            <Text variant="micro" tone="dim">
              Its VM can reach the network, or is down, so it was reviewed
              rather than cleared on its shape.
            </Text>
          </Show>
          <Show when={props.review.risk}>
            {(risk) => (
              <Text variant="micro" tone="dim">
                {RISK[risk()]}
                <Show when={props.review.authorization}>
                  {(authorization) => (
                    <>, and {AUTHORIZATION[authorization()]}.</>
                  )}
                </Show>
              </Text>
            )}
          </Show>
          {/* An observation the operator reads; on its own it refuses nothing. */}
          <Show when={props.review.correctness}>
            {(correctness) => (
              <Text variant="micro" tone="warn">
                {correctness()}
              </Text>
            )}
          </Show>
          <Show when={props.review.scores}>
            {(scores) => (
              <Text variant="micro" tone="dim" class="font-mono">
                {scoreLine(scores())}
              </Text>
            )}
          </Show>
        </div>
      </Collapse>
    </div>
  );
}
