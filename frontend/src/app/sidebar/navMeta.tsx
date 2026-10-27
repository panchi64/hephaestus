import { Show, type JSX } from "solid-js";
import { ACTION_TONES, StatusDot, Text, type Status } from "~/ui";
import { awaitingKind, chatBusy } from "~/lib/stores/chatActivity";
import type { NavArea, NavItem } from "../nav";

/** The item's indicator, with live chat state overlaid on the Chat row. A run
 *  parked on the operator is a stronger signal than plain activity, so it wins
 *  with its ask's tone rather than folding into the busy dot. */
export function itemIndicator(item: NavItem): Status | undefined {
  if (item.href === "/chat") {
    const kind = awaitingKind();
    if (kind) return ACTION_TONES[kind].status;
    if (chatBusy()) return "info";
  }
  return item.indicator;
}

const URGENCY: Status[] = ["alert", "warn", "info", "nominal", "live", "idle"];

/** The strongest indicator anywhere in an area. Without this rollup an area's
 *  activity goes dark the moment the operator navigates away from it — which is
 *  exactly when they need to see it. */
export function areaIndicator(area: NavArea): Status | undefined {
  let best: Status | undefined;
  let bestRank = URGENCY.length;
  for (const item of area.items) {
    const ind = itemIndicator(item);
    if (!ind) continue;
    const rank = URGENCY.indexOf(ind);
    if (rank !== -1 && rank < bestRank) {
      best = ind;
      bestRank = rank;
    }
  }
  return best;
}

export function areaHasOffline(area: NavArea): boolean {
  return area.items.some((i) => !i.connected);
}

function meta(indicator: Status | undefined, offline: boolean, name: string) {
  if (!indicator && !offline) return undefined;
  return (
    <span class="flex items-center gap-2">
      <Show when={indicator}>
        {(ind) => (
          <StatusDot status={ind()} shape="square" label={`${ind()} ${name}`} />
        )}
      </Show>
      <Show when={offline}>
        <Text variant="micro" tone="dim">
          Offline
        </Text>
      </Show>
    </span>
  );
}

/** Right-aligned row meta: an activity square plus a dim OFFLINE tag. The Chat
 *  row always renders the reactive wrapper so it can light up mid-stream. */
export function navMeta(item: NavItem): JSX.Element | undefined {
  if (!item.indicator && item.connected && item.href !== "/chat")
    return undefined;
  return meta(itemIndicator(item), !item.connected, "activity");
}

/** The same meta rolled up to an area, for the switcher. */
export function areaMeta(area: NavArea): JSX.Element | undefined {
  return meta(
    areaIndicator(area),
    areaHasOffline(area),
    `activity in ${area.label}`,
  );
}
