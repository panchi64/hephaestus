import { Show, type JSX } from "solid-js";
import {
  Button,
  Icon,
  REVEAL_ON_GROUP_HOVER,
  StatusDot,
  Text,
  cx,
  type IconName,
} from "~/ui";
import { relativeTime } from "~/lib/format";
import type {
  Notification,
  NotificationKind,
} from "~/lib/stream/notificationEvents";

const KIND_ICON: Record<NotificationKind, IconName> = {
  approval_needed: "warning",
  run_failed: "close",
  run_completed: "check",
  reminder: "bell",
  task_outcome: "activity",
  system: "info",
};

const KIND_TONE: Record<NotificationKind, string> = {
  approval_needed: "text-warn",
  run_failed: "text-alert",
  run_completed: "text-dim",
  reminder: "text-info",
  task_outcome: "text-dim",
  system: "text-dim",
};

/** One notification row. Both lines wrap: truncation cuts an approval or a failure off
 *  right where it says what it wants to run or why. */
export function NotificationRow(props: {
  notification: Notification;
  fading: boolean;
  onOpen: (n: Notification) => void;
  onToggleRead: (n: Notification) => void;
  onDismiss: (n: Notification) => void;
}): JSX.Element {
  const n = () => props.notification;
  const unread = () => !n().readAt;
  const tone = () => (n().resolvedAt ? "text-dim" : KIND_TONE[n().kind]);
  return (
    <div
      class={cx(
        "group relative transition-opacity",
        props.fading && "opacity-0",
      )}
    >
      <button
        type="button"
        onClick={() => props.onOpen(n())}
        class="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-raised"
      >
        <Icon
          name={KIND_ICON[n().kind]}
          size={14}
          class={cx("mt-0.5 shrink-0", tone())}
        />
        <span class="min-w-0 flex-1">
          {/* `items-start` keeps the dot on the title's first line when it wraps. */}
          <span class="flex items-start gap-1.5">
            <Show when={unread()}>
              <StatusDot class="mt-1 shrink-0" status="info" />
            </Show>
            <Text
              variant="label"
              tone={unread() ? "bright" : "dim"}
              class="min-w-0 break-words"
            >
              {n().title}
            </Text>
            <Show when={n().resolvedAt}>
              <Text variant="micro" tone="dim" class="mt-0.5 shrink-0">
                Resolved
              </Text>
            </Show>
          </span>
          <Show when={n().body}>
            <Text
              variant="micro"
              tone="dim"
              class="mt-0.5 block whitespace-pre-wrap break-words"
            >
              {n().body}
            </Text>
          </Show>
        </span>
        <Text
          variant="micro"
          tone="dim"
          class="w-14 shrink-0 whitespace-nowrap text-right transition-opacity group-hover:opacity-0 group-focus-within:opacity-0 no-hover:opacity-0"
        >
          {relativeTime(n().createdAt)}
        </Text>
      </button>
      <div
        class={cx(
          "absolute right-3 top-1 flex w-14 items-center justify-end",
          REVEAL_ON_GROUP_HOVER,
        )}
      >
        <Button
          variant="ghost"
          size="sm"
          leading={unread() ? "check" : "dot"}
          aria-label={unread() ? "Mark as read" : "Mark as unread"}
          onClick={(e) => {
            e.stopPropagation();
            props.onToggleRead(n());
          }}
        />
        <Button
          variant="ghost"
          size="sm"
          leading="close"
          aria-label="Dismiss notification"
          onClick={(e) => {
            e.stopPropagation();
            props.onDismiss(n());
          }}
        />
      </div>
    </div>
  );
}
