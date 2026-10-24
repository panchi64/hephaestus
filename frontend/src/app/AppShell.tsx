import { createEffect, onCleanup, Show, Suspense, type JSX } from "solid-js";
import { useLocation } from "@solidjs/router";
import {
  ErrorBoundary,
  LoadingText,
  NotConnectedOverlay,
  ResizeHandle,
  StatusFlag,
  ThemeToggle,
  Text,
  cx,
} from "~/ui";
import { useSession } from "~/lib/stores/session";
import {
  startNotifications,
  stopNotifications,
} from "~/lib/stores/notifications";
import { useAmbientSignals } from "./ambient/useAmbientSignals";
import { NotificationBell } from "./notifications/NotificationBell";
import { Sidebar, useSidebarWidth } from "./sidebar";
import { isConnectedRoute, isFlushRoute } from "./nav";
import { SettingsDialog } from "./settings-dialog";

/** The authenticated app chrome: sidebar rail + top status bar + the routed
 *  content region. Composed entirely from ~/ui. */
export function AppShell(props: { children: JSX.Element }): JSX.Element {
  const location = useLocation();
  const connected = () => isConnectedRoute(location.pathname);
  const session = useSession();
  const rail = useSidebarWidth();
  useAmbientSignals();

  // The notification feed is app-lifetime, not per-route: subscribe once the
  // workspace is authenticated (the same signal the auth guard gates on) and
  // tear down on logout/lock so no state survives to the next session.
  createEffect(() => {
    if (session.isAuthenticated) startNotifications();
    else stopNotifications();
  });
  onCleanup(stopNotifications);
  return (
    // No fill: the ambient field (§11.1) paints under the whole shell.
    <div class="flex h-screen overflow-hidden text-text">
      {/* The rail is drag-sized; the handle draws the hairline that used to be
          the aside's right border. */}
      {/* `overflow-hidden`, not `overflow-y-auto`: the rail's own body — the
          thread list — owns the scroll now, so a scrollbar here would be a
          second one around it. */}
      <aside
        class="shrink-0 overflow-hidden"
        style={{ width: `${rail.width()}px` }}
      >
        <Sidebar />
      </aside>
      <ResizeHandle
        aria-label="Resize navigation rail"
        onResize={rail.resize}
        onResizeEnd={rail.persist}
      />

      <div class="relative flex min-w-0 flex-1 flex-col">
        {/* No rule under the top bar (§7). It is already a distinct region by
            position and by what it holds; a full-width hairline across every
            screen was chrome drawn where the eye had found the break. */}
        <header class="relative flex shrink-0 items-center justify-between gap-3 px-4 py-2">
          <div class="flex items-center gap-3">
            <StatusFlag status="live" dot>
              Link
            </StatusFlag>
            <Text variant="micro" tone="dim">
              Local
            </Text>
          </div>
          {/* No model picker here any more — it moved to the composer's action row
              (`~/app/ModelPicker`). The model is a property of the message being
              sent, not of the application, and a persistent global control sat on
              every screen including the many with nothing to send. */}
          <div class="flex items-center gap-3">
            <NotificationBell />
            <ThemeToggle />
          </div>
        </header>

        {/* The registration marks moved off the shell and onto `PageHeader`
            (§9). Framing the content region put two crosses just above the page
            and two in the bottom corners of the screen — bracketing the window
            rather than anything in it. They now frame the header row, which is
            an actual object. */}
        {/* No z-index: a stacking context here risks making this a backdrop root
            in some engines, which would leave the glass surfaces inside it
            blurring nothing (see the note on `.heph-glass`). */}
        <div class="relative min-h-0 flex-1">
          {/* Scopes a suspending *or throwing* screen to the content region;
              without them the root blanks the shell too, taking the rail and
              the status bar with it. The path is the reset key, so navigating
              away from a broken screen clears the error. */}
          <main
            class={cx(
              "h-full overflow-y-auto px-6",
              // The top and bottom insets are the ones a self-framing screen takes
              // back — see `isFlushRoute`. Everything else keeps the even margin its
              // registration marks are drawn against.
              isFlushRoute(location.pathname) ? "py-0" : "py-6",
            )}
          >
            <ErrorBoundary resetKey={() => location.pathname}>
              <Suspense fallback={<LoadingText label="Loading" />}>
                {props.children}
              </Suspense>
            </ErrorBoundary>
          </main>
          <Show when={!connected()}>
            <NotConnectedOverlay />
          </Show>
        </div>
      </div>

      {/* Mounted once, here, and reading its own open state from the URL. It
          portals to the body, so where it sits in this tree decides nothing
          about where it paints — only that it exists on every route, which is
          what lets `?settings=…` open it over any of them. */}
      <SettingsDialog />
    </div>
  );
}
