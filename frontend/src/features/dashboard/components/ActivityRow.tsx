import { Show, type JSX } from "solid-js";
import { Dynamic } from "solid-js/web";
import { MetClock, StatusDot, StatusFlag, Text, cx } from "~/ui";
import { relativeTime } from "~/lib/format";
import { useNow } from "~/lib/useNow";
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
  // Relative times only need to age by the minute, on the ticker the page shares.
  const now = useNow(60_000);
  const spec = () =>
    props.item.state ? runStateSpec(props.item.state) : undefined;
  const loud = () => spec()?.loud ?? false;
  const ticking = () =>
    props.item.state === "running" && props.item.startedAt !== undefined;
  // A live run says what it is doing; at rest the row says where it ran.
  const where = () =>
    (spec()?.live && props.item.detail) ||
    [props.item.workspace, props.item.model].filter(Boolean).join(" · ");

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
      <StatusDot status={loud() ? spec()!.status : "idle"} pulse={ticking()} />
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
        {/* The square is colour alone, so the state is said in words too (§12). */}
        <Show when={spec()}>
          {(s) => <span class="sr-only">{`, ${s().spoken}`}</span>}
        </Show>
      </span>
      <Text variant="micro" tone="dim" class="hidden truncate md:block">
        {where()}
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
                {props.item.updatedAt
                  ? relativeTime(props.item.updatedAt, new Date(now()))
                  : ""}
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
