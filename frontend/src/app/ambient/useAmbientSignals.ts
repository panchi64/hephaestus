import { createEffect, on, onCleanup } from "solid-js";
import type { PulseTone } from "~/ui";
import { useChatSessions } from "~/features/chat/data";
import { pushPulse, setAmbientLoad } from "~/lib/stores/ambient";
import { awaitingInput, chatBusy } from "~/lib/stores/chatActivity";
import { useNotifications } from "~/lib/stores/notifications";
import type { NotificationKind } from "~/lib/stream/notificationEvents";
import { activityLoad, diffActivity } from "./activity";
import {
  bellSource,
  isOpenThread,
  openThreadSource,
  threadSource,
} from "./sources";

/** Kinds that ring from the bell. Approvals and run outcomes are already rung from the
 *  thread list, at the thread's own row. */
const BELL_KINDS: ReadonlySet<NotificationKind> = new Set([
  "reminder",
  "task_outcome",
  "system",
]);

/** How long the open thread's own ring stands in for the thread list's. The list is
 *  re-read when a send starts, usually before the backend has registered the run, so
 *  it reports the start one poll later. */
const OWN_RING_MS = 30_000;

/** Feeds the ambient field from the open thread's stream, the thread list and the
 *  notification feed. Call once, inside the authenticated shell. */
export function useAmbientSignals(): void {
  const sessions = useChatSessions();
  const notifications = useNotifications();
  const ownRang = new Map<PulseTone, number>();

  // Signed out, nothing is running as far as the field can say.
  onCleanup(() => setAmbientLoad(0));

  const ringOpenThread = (tone: PulseTone): void => {
    ownRang.set(tone, performance.now());
    const at = openThreadSource(tone === "warn");
    pushPulse(at.x, at.y, tone);
  };
  createEffect(
    on(chatBusy, (busy) => busy && ringOpenThread("info"), { defer: true }),
  );
  createEffect(
    on(awaitingInput, (waiting) => waiting && ringOpenThread("warn"), {
      defer: true,
    }),
  );

  // The open thread's own run counts before the list catches up with it.
  createEffect(() => {
    const own = (chatBusy() ? 1 : 0) + (awaitingInput() ? 1 : 0);
    setAmbientLoad(Math.max(activityLoad(sessions() ?? []), own));
  });

  let previous: Parameters<typeof diffActivity>[0];
  createEffect(() => {
    const rows = sessions();
    if (!rows) return;
    const { pulses, snapshot } = diffActivity(previous, rows);
    previous = snapshot;
    const now = performance.now();
    for (const p of pulses) {
      const own = ownRang.get(p.tone);
      if (
        own !== undefined &&
        now - own < OWN_RING_MS &&
        isOpenThread(p.threadId)
      )
        continue;
      const at = threadSource(p.threadId, p.tone === "warn");
      pushPulse(at.x, at.y, p.tone);
    }
  });

  createEffect(
    on(
      () => notifications.lastCreated,
      (n) => {
        if (!n || !BELL_KINDS.has(n.kind)) return;
        const at = bellSource();
        pushPulse(at.x, at.y, "neutral");
      },
      { defer: true },
    ),
  );
}
