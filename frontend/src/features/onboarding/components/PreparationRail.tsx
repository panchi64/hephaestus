import { For, type JSX } from "solid-js";
import { RailRow, StatusBar, Text } from "~/ui";
import { LockCell, RailFrame } from "~/app/sidebar";
import { STEPS, stepCounter, stepIndex, type StepId } from "../model";

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
        </div>
        <RailRow
          title="Vault key derived"
          readout="DONE"
          onOpen={() => undefined}
        />
        <RailRow
          title="Local scan"
          state={props.scanning ? "running" : "idle"}
          readout={props.scanning ? "SCANNING" : `${props.found} FOUND`}
          onOpen={() => undefined}
        />
        <For each={STEPS}>
          {(step) => {
            const entry = () => props.entries[step.id];
            const upcoming = () =>
              !entry().done && stepIndex(step.id) > stepIndex(props.current);
            return (
              <RailRow
                title={
                  <span class={upcoming() ? "text-dim" : undefined}>
                    {step.label}
                  </span>
                }
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
