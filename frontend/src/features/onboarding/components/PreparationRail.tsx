import { For, type JSX } from "solid-js";
import { RailRow, StatusBar, Text, type RailRowState } from "~/ui";
import { LockCell, RailFrame } from "~/app/sidebar";
import { STEPS, stepCounter, type StepId } from "../model";

/** The two rows done unasked, plus every step but Review, which is left by entering. */
const TOTAL = STEPS.filter((s) => s.id !== "ready").length + 2;

export interface RailEntry {
  /** What the row reads at its right, in the machine voice. */
  readout: string;
  done: boolean;
}

/** The quiet checklist: what was done unasked, then one row per step. A row can be
 *  reached once every required step before it is done. */
export function PreparationRail(props: {
  current: StepId;
  scanning: boolean;
  found: number;
  entries: Record<StepId, RailEntry>;
  reachable: (step: StepId) => boolean;
  onGo: (step: StepId) => void;
}): JSX.Element {
  const stateOf = (id: StepId): RailRowState => {
    if (id === props.current) return "current";
    return props.entries[id].done ? "done" : "pending";
  };
  // The two rows done unasked count toward the total, so the count never reads zero
  // on a walkthrough that has already started working.
  const doneCount = (): number =>
    2 + STEPS.filter((s) => props.entries[s.id].done).length;

  return (
    <RailFrame
      assetId="heph-setup-01.0"
      footer={
        <StatusBar
          variant="recede"
          start={
            <Text variant="micro" tone="dim" class="flex-1 px-3 font-mono">
              {stepCounter(props.current)}
            </Text>
          }
          end={<LockCell />}
        />
      }
    >
      <div class="min-h-0 flex-1 overflow-y-auto">
        <div class="heph-list-plate" data-rule="">
          <span class="heph-list-plate-label">Preparation</span>
          <span class="heph-list-plate-count">
            {doneCount()}/{TOTAL}
          </span>
        </div>
        <RailRow
          title="Workspace encrypted"
          state="done"
          readout="DONE"
          onOpen={() => undefined}
        />
        <RailRow
          title="Looked for local models"
          state={props.scanning ? "running" : "done"}
          readout={props.scanning ? "SCANNING" : `${props.found} FOUND`}
          onOpen={() => undefined}
        />
        <For each={STEPS}>
          {(step) => {
            const entry = () => props.entries[step.id];
            return (
              <RailRow
                title={step.label}
                state={stateOf(step.id)}
                selected={step.id === props.current}
                readout={entry().readout}
                onOpen={() => {
                  if (props.reachable(step.id)) props.onGo(step.id);
                }}
              />
            );
          }}
        </For>
      </div>
    </RailFrame>
  );
}
