import { Show, createSignal, onCleanup, type JSX } from "solid-js";
import { api } from "~/lib/api";
import { streamRun, type RunEvent } from "~/lib/stream";
import { ActionButtons, ActionFrame, LoadingText, toast } from "~/ui";
import type { Approval, ApprovalDecision } from "../model";
import type { Subagent } from "../data";
import { isOneClickPark } from "../approvalDecisions";
import { approvalFromEvent } from "../approvalEvent";
import { ApprovalPanel } from "./ApprovalPanel";
import { SubagentDesignation } from "./SubagentDesignation";

/**
 * A sub-agent that stopped to ask permission, answered where the operator finds it: no
 * one is in that thread to type in, so the run would otherwise deadlock.
 *
 * Reuses existing machinery on a different run: the approval endpoint is run-id
 * addressed, and `ApprovalPanel` is the main room's card so the same act reads
 * identically wherever it is answered.
 *
 * Reads the child's own stream from seq 0, since a parked run's requests live in its
 * event log (as on a main-room reload). That needs no backend field: the card's status
 * says it is blocked and the run says on what. Opened only while a blocked card is
 * expanded.
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
            : [...prev, approvalFromEvent(event)],
        );
      }
      // The replay carries both frames for approvals settled in an earlier park of the same run.
      if (event.type === "tool.completed") {
        setApprovals((prev) =>
          prev.filter((a) => a.toolCallId !== event.tool_call_id),
        );
      }
    },
  }).catch(() => {
    // Not reported: the card still says blocked, and the next poll re-opens this.
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
    // The action tier, same frame as the dock: the one sub-agent surface that is a request.
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
          // One call settles on the click; several keep pick-then-send.
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
