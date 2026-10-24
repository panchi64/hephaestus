import { For, Show, type JSX } from "solid-js";
import { EmptyState, Text } from "~/ui";
import type { Activity } from "../activity";
import { ActivityRow } from "./ActivityRow";

/** Live work and recent threads as one ruled list. Bare (§10.2): a plate legend and
 *  row rules, no frame or fill, so it reads as a readout behind the composer. */
export function ActivityLedger(props: {
  activity: Activity;
  onOpen: (conversationId: string) => void;
}): JSX.Element {
  return (
    <section aria-label="Activity">
      <div class="flex items-center justify-between border-b border-line px-4 pb-2">
        <Text variant="plate" tone="dim">
          Activity
        </Text>
        <Show when={props.activity.total > 0}>
          <Text variant="plate" tone="dim">
            {`${props.activity.items.length} of ${props.activity.total}`}
          </Text>
        </Show>
      </div>
      <Show
        when={props.activity.items.length}
        fallback={
          <EmptyState
            icon="terminal"
            message="No conversations yet"
            hint="Start one above to see it here."
          />
        }
      >
        <For each={props.activity.items}>
          {(item) => (
            <ActivityRow
              item={item}
              onOpen={
                item.conversationId
                  ? () => props.onOpen(item.conversationId!)
                  : undefined
              }
            />
          )}
        </For>
      </Show>
    </section>
  );
}
