import { Show, createSignal, onCleanup, type JSX } from "solid-js";
import { api } from "~/lib/api";
import { streamRun, type RunEvent } from "~/lib/stream";
import { ActionButtons, ActionFrame, LoadingText, toast } from "~/ui";
import type { Approval, ApprovalDecision } from "../model";
import type { Subagent } from "../data";
import { isOneClickPark } from "../approvalDecisions";
import { ApprovalPanel } from "./ApprovalPanel";
import { SubagentDesignation } from "./SubagentDesignation";

/**
 * A sub-agent that stopped to ask permission, answered where the operator finds it.
 *
 * Without this a blocked sub-agent is a deadlock with a spinner on it: nobody is in that
 * thread, there is nowhere to type in it, and the run waits for a decision the operator
 * has no way to give. The panel is where they learn about it, so it has to be where they
 * answer it.
 *
 * **Everything here is the existing machinery, addressed at a different run.** The
 * approval endpoint is already run-id addressed, so a sub-agent's park needs no route of
 * its own; `ApprovalPanel` is the same card the main room renders, so an approval reads
 * identically wherever it is answered — which matters, because the operator is ruling on
 * the same act either way and a second, thinner card would be one that quietly showed
 * less about it.
 *
 * **Read off the child's own stream, from the beginning.** A parked run's requests live
 * in its event log, replayed in full from seq 0 — the same replay the main room does
 * after a reload, for the same reason. That also means this needs no new backend field:
 * the fact that a sub-agent is blocked comes from the card's status, and *what* it is
 * blocked on comes from the run that is blocked.
 *
 * The stream is opened only while a blocked card is expanded, and closed with it.
 */
export function SubagentApproval(props: {
  subagent: Subagent;
  /** Called once the run has been resumed, so the panel can re-read its cards. */
  onSettled: () => void;
}): JSX.Element {
  const [approvals, setApprovals] = createSignal<Approval[]>([]);
  const [decisions, setDecisions] = createSignal<ApprovalDecision[]>([]);
  const [ready, setReady] = createSignal(false);
  const [busy, setBusy] = createSignal(false);

  const controller = new AbortController();
  onCleanup(() => controller.abort());

  void streamRun(props.subagent.runId, {
    fromSeq: 0,
    signal: controller.signal,
    onEvent: (event: RunEvent) => {
      if (event.type === "approval.required") {
        setApprovals((prev) =>
          prev.some((a) => a.toolCallId === event.tool_call_id)
            ? prev
            : [
                ...prev,
                {
                  toolCallId: event.tool_call_id,
                  name: event.name,
                  args: event.args ?? {},
                  summary: event.summary,
                  explanation: event.explanation ?? undefined,
                  guard: event.guard ?? undefined,
                  unconfined: event.unconfined ?? undefined,
                },
              ],
        );
      }
      // A call that produced a result is waiting on nobody — the replay carries both
      // frames for every approval already settled in an earlier park of the same run.
      if (event.type === "tool.completed") {
        setApprovals((prev) =>
          prev.filter((a) => a.toolCallId !== event.tool_call_id),
        );
      }
    },
  }).catch(() => {
    // A detached stream is not a failure to report here: the card still says the
    // sub-agent is blocked, and the next poll re-opens this.
  });

  const submit = async (settled?: ApprovalDecision[]): Promise<void> => {
    if (busy()) return;
    setBusy(true);
    try {
      await api.post(`/runs/${props.subagent.runId}/approve`, {
        decisions: settled ?? decisions(),
        answers: [],
      });
      props.onSettled();
    } catch {
      toast.error(
        "That decision could not be delivered — the sub-agent may have ended.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    // The action tier (design §10.18), the same frame the dock wears: the one
    // sub-agent surface that is a request rather than a report.
    <ActionFrame
      tone="approval"
      title={
        <>
          Approval needed · <SubagentDesignation subagent={props.subagent} />{" "}
          {props.subagent.handle}
        </>
      }
    >
      <Show
        when={approvals().length > 0}
        fallback={
          <div class="px-3 py-3">
            <LoadingText label="Reading what it is asking for…" />
          </div>
        }
      >
        <ApprovalPanel
          approvals={approvals()}
          // One call is one click: the verdict is sent on the spot and there is no
          // second button to press. Several keep the pick-then-send flow.
          onSettle={
            isOneClickPark(approvals().length, 0)
              ? (given) => submit(given)
              : undefined
          }
          onChange={(next, allDecided) => {
            setDecisions(next);
            setReady(allDecided);
          }}
        />
        <Show
          when={
            !isOneClickPark(approvals().length, 0) ||
            decisions().some((d) => d.intent === "revise")
          }
        >
          <div class="flex justify-end border-t border-line">
            <ActionButtons
              buttons={[
                {
                  label: busy() ? "Sending…" : "Send decision",
                  primary: true,
                  disabled: !ready() || busy(),
                  onClick: () => void submit(),
                },
              ]}
            />
          </div>
        </Show>
      </Show>
    </ActionFrame>
  );
}
