import { Show, type JSX } from "solid-js";
import { Button, Row, StatusFlag, Text } from "~/ui";
import { AllowHostContext } from "../allowHostContext";
import { allowDomain } from "../data/egress";
import type { Subagent, SubagentStatus } from "../data";
import { SubagentApproval } from "./SubagentApproval";
import { SubagentDesignation } from "./SubagentDesignation";
import { SubagentTranscript } from "./SubagentTranscript";

/** The same reading as the card's, in the flag's vocabulary. `blocked` is the only one
 *  that is a request rather than a report, so it is the only one that warns. */
const FLAG: Record<SubagentStatus, "nominal" | "warn" | "alert" | "idle"> = {
  running: "nominal",
  blocked: "warn",
  done: "nominal",
  failed: "alert",
  cancelled: "idle",
  stopped: "idle",
};

const LABEL: Record<SubagentStatus, string> = {
  running: "Working",
  blocked: "Waiting for you",
  done: "Done",
  failed: "Failed",
  cancelled: "Stopped",
  stopped: "Stopped at a limit",
};

/**
 * One sub-agent, filling the panel: its brief, its approval if it is stuck on one, and
 * its transcript.
 *
 * It takes the whole pane instead of unfolding under its card: a live transcript grows
 * on every poll and would slide the next row out from under the cursor. The way back is
 * a button.
 */
export function SubagentDetail(props: {
  subagent: Subagent;
  /** Back to the list of sub-agents. */
  onBack: () => void;
  /** Re-read the cards — after a decision the operator just gave this one. */
  onSettled?: () => void;
}): JSX.Element {
  return (
    <div class="flex h-full min-h-0 flex-col gap-2">
      <div class="flex shrink-0 flex-col gap-1">
        <Row gap={2} align="center" wrap>
          <Button
            variant="ghost"
            size="sm"
            leading="chevron-left"
            onClick={() => props.onBack()}
          >
            All agents
          </Button>
          <StatusFlag status={FLAG[props.subagent.status]} dot>
            {LABEL[props.subagent.status]}
          </StatusFlag>
        </Row>
        <Row gap={2} align="center" wrap>
          <SubagentDesignation subagent={props.subagent} />
          <Text variant="label" tone="bright">
            {props.subagent.handle}
          </Text>
        </Row>
        {/* The brief in full; first thing to read when a sub-agent answered the wrong question. */}
        <Text variant="micro" tone="dim" class="block break-words">
          {props.subagent.name} · {props.subagent.task}
        </Text>
      </div>

      <Show when={props.subagent.status === "blocked"}>
        <div class="shrink-0">
          <SubagentApproval
            subagent={props.subagent}
            onSettled={() => props.onSettled?.()}
          />
        </div>
      </Show>

      <div class="min-h-0 flex-1 overflow-y-auto">
        {/* Its commands run in its own thread, so an allowed host applies to that thread. */}
        <AllowHostContext.Provider
          value={async (host) => {
            await allowDomain(props.subagent.conversationId, host);
          }}
        >
          <SubagentTranscript subagent={props.subagent} />
        </AllowHostContext.Provider>
      </div>
    </div>
  );
}
