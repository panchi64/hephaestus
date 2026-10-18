import { Show, type JSX } from "solid-js";
import { Dynamic } from "solid-js/web";
import { MetClock, StatusDot, StatusFlag, Text, cx } from "~/ui";
import { relativeTime } from "~/lib/format";
import { runStateSpec } from "~/features/chat/runState";
import type { ActivityItem } from "../activity";

/** State, title, where it ran, readout. The where column gives way below `md`, where
 *  the title needs the width. `px-4` is the composer's text edge, which every line on
 *  the launchpad shares. */
const ACTIVITY_COLUMNS =
  "grid grid-cols-[8px_minmax(0,1fr)_104px] md:grid-cols-[8px_minmax(0,1fr)_176px_104px] items-center gap-3 px-4";

/** One ledger row (§10.7): hairline beneath, sans title, mono machine columns. Only a
 *  state worth marking from across the room takes a hue (`runStateSpec().loud`), so a
 *  ledger of finished threads stays grayscale (§5). */
export function ActivityRow(props: {
  item: ActivityItem;
  onOpen?: () => void;
}): JSX.Element {
  const spec = () =>
    props.item.state ? runStateSpec(props.item.state) : undefined;
  const loud = () => spec()?.loud ?? false;
  const ticking = () =>
    props.item.state === "running" && props.item.startedAt !== undefined;

  return (
    <Dynamic
      component={props.onOpen ? "button" : "div"}
      type={props.onOpen ? "button" : undefined}
      onClick={props.onOpen}
      class={cx(
        ACTIVITY_COLUMNS,
        "h-8 w-full border-b border-line text-left outline-none",
        props.onOpen && "hover:bg-raised focus-visible:bg-raised",
      )}
    >
      <StatusDot
        status={loud() ? spec()!.status : "idle"}
        pulse={ticking()}
        label={spec()?.spoken}
      />
      <span class="flex min-w-0 items-baseline gap-2">
        <Text
          variant="body"
          tone={spec()?.live ? "bright" : "default"}
          class="truncate"
        >
          {props.item.title}
        </Text>
        <Show when={props.item.agentName}>
          {(name) => (
            <Text variant="micro" tone="dim" class="shrink-0">
              {name()}
            </Text>
          )}
        </Show>
      </span>
      <Text variant="micro" tone="dim" class="hidden truncate md:block">
        {[props.item.workspace, props.item.model].filter(Boolean).join(" · ")}
      </Text>
      <span class="flex justify-end whitespace-nowrap">
        <Show
          when={!ticking()}
          fallback={
            <MetClock startedAt={props.item.startedAt} variant="micro" />
          }
        >
          <Show
            when={loud() && spec()}
            fallback={
              <Text variant="micro" tone="dim">
                {props.item.updatedAt ? relativeTime(props.item.updatedAt) : ""}
              </Text>
            }
          >
            {(s) => <StatusFlag status={s().status}>{s().readout}</StatusFlag>}
          </Show>
        </Show>
      </span>
    </Dynamic>
  );
}
