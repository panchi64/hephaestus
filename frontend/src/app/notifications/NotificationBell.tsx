import { Show, type JSX } from "solid-js";
import { Button, Icon, Popover } from "~/ui";
import { useNotifications } from "~/lib/stores/notifications";
import { NotificationPanel } from "./NotificationPanel";

/** Sidebar bell with an unread badge, opening the notification panel. Read state and
 *  dedupe are backend-owned; this only renders the store. */
export function NotificationBell(): JSX.Element {
  const notifications = useNotifications();

  const badge = () => {
    const c = notifications.unreadCount;
    return c > 9 ? "9+" : String(c);
  };

  return (
    <Popover
      align="right"
      // Rows wrap rather than clip, so the panel is wide enough to keep them a few lines tall.
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
            <span class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center bg-bright px-0.5 text-micro font-mono font-bold text-bg">
              {badge()}
            </span>
          </Show>
        </Button>
      )}
      panel={({ close }) => <NotificationPanel close={close} />}
    />
  );
}
