import { For, Show, type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import {
  Button,
  EmptyState,
  Icon,
  Popover,
  StatusDot,
  Text,
  cx,
  type IconName,
} from "~/ui";
import { relativeTime } from "~/lib/format";
import { useNotifications } from "~/lib/stores/notifications";
import type {
  Notification,
  NotificationKind,
} from "~/lib/stream/notificationEvents";
import { openConversation } from "~/features/chat/data";

/** Icon + accent per kind (design: approval_needed = warn, run_failed = danger,
 *  run_completed/task_outcome = neutral, reminder = info accent). */
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

/** One notification row: kind icon, title + optional body, relative time, and
 *  unread emphasis (brighter title, a leading dot). Clicking marks it read and
 *  — when it references a conversation — navigates there; the chat screen's
 *  cold-load/reattach machinery takes it from there.
 *
 *  **Both lines wrap; neither truncates.** They used to clip to one line each, which for
 *  the notifications that matter most — an approval naming what it wants to run, a run
 *  that failed and said why — cut the message exactly where it started to say something.
 *  The panel is a scrolling list and a tall row costs it nothing; an unreadable one costs
 *  it the reason it exists. */
function NotificationRow(props: {
  notification: Notification;
  onOpen: (n: Notification) => void;
}): JSX.Element {
  const n = () => props.notification;
  const unread = () => !n().readAt;
  return (
    <button
      type="button"
      onClick={() => props.onOpen(n())}
      class="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-raised"
    >
      <Icon
        name={KIND_ICON[n().kind]}
        size={14}
        class={cx("mt-0.5 shrink-0", KIND_TONE[n().kind])}
      />
      <span class="min-w-0 flex-1">
        {/* `items-start`, so the unread dot marks the title's first line rather than
            floating to the middle of a wrapped one. */}
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
      <Text variant="micro" tone="dim" class="shrink-0 whitespace-nowrap">
        {relativeTime(n().createdAt)}
      </Text>
    </button>
  );
}

/** Sidebar bell + unread badge, opening a newest-first notification panel — the
 *  durable attention surface (design: approval/failure/completion/task-outcome
 *  notifications land here even when the operator was elsewhere). Read/unread,
 *  emit, and dedupe policy are entirely backend-owned; this only renders the
 *  store built by the previous batch (`~/lib/stores/notifications`) and relays
 *  mark-read/click intent back to it. */
export function NotificationBell(): JSX.Element {
  const notifications = useNotifications();
  const navigate = useNavigate();

  const badge = () => {
    const c = notifications.unreadCount;
    return c > 9 ? "9+" : String(c);
  };

  const openNotification = (n: Notification) => {
    // A park needs no deep-link focus intent: opening the thread is enough, because
    // the dock holds the composer's slot and is on screen already.
    void notifications.markRead([n.id]);
    if (n.conversationId) {
      openConversation(n.conversationId);
      navigate("/chat");
    }
  };

  return (
    <Popover
      align="right"
      // Wider than it was, because the rows now wrap rather than clip (see
      // `NotificationRow`): at 80 a two-clause approval line became six short ones.
      panelClass="w-96 max-h-96 flex flex-col overflow-hidden"
      trigger={({ open, setOpen }) => (
        <Button
          variant="ghost"
          size="sm"
          class="relative"
          aria-label="Notifications"
          onClick={() => setOpen(!open())}
        >
          <Icon name="bell" size={14} />
          <Show when={notifications.unreadCount > 0}>
            {/* `radius-full`, not the control radius: §7 gives the pill form to
                status dots and count pills, and this is the second of the two.
                At 16px a 3px corner is a fifth of the box — neither a chamfer
                nor a pill, just a square that lost its corners. */}
            <span class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center bg-alert px-0.5 text-micro font-mono font-bold text-bg">
              {badge()}
            </span>
          </Show>
        </Button>
      )}
      panel={({ close }) => (
        <div class="flex max-h-96 flex-col">
          <div class="shrink-0">
            <div class="flex items-center justify-between gap-2 px-3 py-2">
              <Text variant="micro" tone="dim">
                Notifications
              </Text>
              <Show when={notifications.unreadCount > 0}>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void notifications.markAllRead()}
                >
                  Mark all read
                </Button>
              </Show>
            </div>
          </div>
          <div class="min-h-0 flex-1 overflow-y-auto">
            <Show
              when={notifications.items.length > 0}
              fallback={<EmptyState icon="bell" message="No notifications" />}
            >
              <For each={notifications.items}>
                {(n) => (
                  <NotificationRow
                    notification={n}
                    onOpen={(item) => {
                      close();
                      openNotification(item);
                    }}
                  />
                )}
              </For>
              {/* The way to whatever the badge is counting beyond this page. It
                  sits at the end of the list rather than in the header, because
                  it is the list continuing — the same gesture as scrolling. */}
              <Show when={notifications.hasOlder}>
                <div class="px-3 py-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    class="w-full"
                    disabled={notifications.loadingOlder}
                    onClick={() => void notifications.loadOlder()}
                  >
                    {notifications.loadingOlder ? "Loading…" : "Load older"}
                  </Button>
                </div>
              </Show>
            </Show>
          </div>
        </div>
      )}
    />
  );
}
