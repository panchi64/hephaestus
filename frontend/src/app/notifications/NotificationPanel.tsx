import {
  createMemo,
  createSignal,
  For,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { useNavigate } from "@solidjs/router";
import { Button, EmptyState, Tabs, Text } from "~/ui";
import { useNotifications } from "~/lib/stores/notifications";
import type { Notification } from "~/lib/stream/notificationEvents";
import { openConversation } from "~/features/chat/data";
import { groupByDay } from "./groupByDay";
import { NotificationRow } from "./NotificationRow";

type Tab = "unread" | "all";

/** Matches the rows' opacity transition, which reduced motion collapses to 0. */
const FADE_MS = 180;

export function NotificationPanel(props: { close: () => void }): JSX.Element {
  const notifications = useNotifications();
  const navigate = useNavigate();
  const [tab, setTab] = createSignal<Tab>("unread");
  // Rows read since the panel opened stay on the Unread tab, so acting on one
  // doesn't move the list under the pointer.
  const [seen, setSeen] = createSignal<ReadonlySet<string>>(new Set());
  const [leaving, setLeaving] = createSignal(false);
  let fadeTimer: ReturnType<typeof setTimeout> | undefined;
  onCleanup(() => clearTimeout(fadeTimer));

  const unreadRows = createMemo(() =>
    notifications.items.filter((n) => !n.readAt || seen().has(n.id)),
  );
  const groups = createMemo(() => groupByDay(notifications.items, new Date()));
  const hasRead = () => notifications.items.some((n) => n.readAt);

  const remember = (ids: string[]) =>
    setSeen((prev) => new Set<string>([...prev, ...ids]));

  const open = (n: Notification) => {
    remember([n.id]);
    void notifications.markRead([n.id]);
    props.close();
    if (n.conversationId) {
      openConversation(n.conversationId);
      navigate("/chat");
    }
  };

  const toggleRead = (n: Notification) => {
    if (n.readAt) {
      void notifications.markUnread([n.id]);
      return;
    }
    remember([n.id]);
    void notifications.markRead([n.id]);
  };

  const markAllRead = () => {
    remember(unreadRows().map((n) => n.id));
    setLeaving(true);
    void notifications.markAllRead();
    const reduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(
      () => {
        setSeen(new Set<string>());
        setLeaving(false);
      },
      reduced ? 0 : FADE_MS,
    );
  };

  const row = (n: Notification, fading: boolean) => (
    <NotificationRow
      notification={n}
      fading={fading}
      onOpen={open}
      onToggleRead={toggleRead}
      onDismiss={(item) => void notifications.dismiss([item.id])}
    />
  );

  return (
    <div class="flex max-h-96 flex-col">
      <Tabs
        class="shrink-0 px-2 py-1.5"
        items={[
          { value: "unread", label: `Unread (${notifications.unreadCount})` },
          { value: "all", label: "All" },
        ]}
        value={tab()}
        onChange={(v) => setTab(v === "all" ? "all" : "unread")}
        trailing={
          <>
            <Show when={tab() === "unread" && notifications.unreadCount > 0}>
              <Button variant="ghost" size="sm" onClick={markAllRead}>
                Mark all read
              </Button>
            </Show>
            <Show when={tab() === "all" && hasRead()}>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void notifications.clearRead()}
              >
                Clear read
              </Button>
            </Show>
          </>
        }
      />
      <div class="min-h-0 flex-1 overflow-y-auto">
        <Show
          when={tab() === "all"}
          fallback={
            <Show
              when={unreadRows().length > 0}
              fallback={<EmptyState icon="check" message="All caught up" />}
            >
              <For each={unreadRows()}>{(n) => row(n, leaving())}</For>
            </Show>
          }
        >
          <Show
            when={notifications.items.length > 0}
            fallback={<EmptyState icon="bell" message="No notifications" />}
          >
            <For each={groups()}>
              {(group) => (
                <section>
                  <Text variant="micro" tone="dim" class="block px-3 pb-1 pt-2">
                    {group.label}
                  </Text>
                  <For each={group.items}>{(n) => row(n, false)}</For>
                </section>
              )}
            </For>
          </Show>
        </Show>
        {/* The list continuing past the page held, which the badge may be counting. */}
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
      </div>
    </div>
  );
}
