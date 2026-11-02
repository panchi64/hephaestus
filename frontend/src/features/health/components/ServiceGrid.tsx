import { For, Show, type JSX } from "solid-js";
import { Icon, Panel, StatusFlag, Text } from "~/ui";
import { relativeTime } from "~/lib/format";
import type { ServiceHealth } from "../model";
import { healthFlag } from "./status";

/** One panel of service rows. Every row is a subgrid of the panel's grid, so the
 *  detail and status columns line up however long a label runs. */
export function ServiceGrid(props: {
  label: string;
  services: ServiceHealth[];
  onSelect: (svc: ServiceHealth) => void;
}): JSX.Element {
  return (
    <Panel label={props.label} flush>
      <div class="grid grid-cols-[auto_1fr_auto]">
        <For each={props.services}>
          {(svc) => (
            <button
              type="button"
              onClick={() => props.onSelect(svc)}
              class="col-span-full grid grid-cols-subgrid cursor-pointer items-center gap-x-3 border-b border-line px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-raised"
            >
              <span class="flex min-w-0 items-center gap-2">
                <Icon
                  name={svc.kind === "endpoint" ? "database" : "activity"}
                  class="shrink-0 text-dim"
                />
                <Text variant="label" class="truncate">
                  {svc.label}
                </Text>
              </span>
              <span class="flex min-w-0 items-center justify-end gap-3">
                <Text variant="micro" tone="dim" class="truncate">
                  {svc.detail}
                </Text>
                <Show when={svc.checkedAt}>
                  {(at) => (
                    <Text variant="micro" tone="dim" class="shrink-0">
                      {relativeTime(at())}
                    </Text>
                  )}
                </Show>
              </span>
              <StatusFlag status={healthFlag[svc.status]} dot>
                {svc.status.toUpperCase()}
              </StatusFlag>
            </button>
          )}
        </For>
      </div>
    </Panel>
  );
}
