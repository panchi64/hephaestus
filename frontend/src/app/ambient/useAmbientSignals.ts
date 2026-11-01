import { createEffect, on, onCleanup } from "solid-js";
import { ACTION_TONES, strikePoint, type StrikeTone } from "~/ui";
import { useChatSessions } from "~/features/chat/data";
import {
  pushStrike,
  setAmbientAurora,
  setAmbientLoad,
} from "~/lib/stores/ambient";
import {
  awaitingInput,
  awaitingKind,
  chatBusy,
  chatThreadId,
} from "~/lib/stores/chatActivity";
import { useNotifications } from "~/lib/stores/notifications";
import type { NotificationKind } from "~/lib/stream/notificationEvents";
import { activityLoad, diffActivity, liveSubagents } from "./activity";

/** Kinds that strike through the bell. Approvals and run outcomes already strike
 *  from the thread list. */
const BELL_KINDS: ReadonlySet<NotificationKind> = new Set([
  "reminder",
  "task_outcome",
  "system",
]);

/** How long the open thread's own strike stands in for the thread list's. The list is
 *  re-read when a send starts, usually before the backend has registered the run, so
 *  it reports the start one poll later. */
const OWN_STRIKE_MS = 30_000;

const strike = (tone: StrikeTone): void => {
  const at = strikePoint(window.innerWidth, window.innerHeight);
  pushStrike(at.x, at.y, tone);
};

/** Feeds the ambient field from the open thread's stream, the thread list and the
 *  notification feed. Call once, inside the authenticated shell. */
export function useAmbientSignals(): void {
  const sessions = useChatSessions();
  const notifications = useNotifications();
  const ownStruck = new Map<StrikeTone, number>();

  // Signed out, nothing is running as far as the field can say.
  onCleanup(() => {
    setAmbientLoad(0);
    setAmbientAurora(0);
  });

  const openRow = () => {
    const id = chatThreadId();
    return id ? sessions()?.find((s) => s.id === id) : undefined;
  };
  // A spawned thread open in the room shows as aurora, never as lightning or load.
  const openIsSpawned = () => !!openRow()?.parentId;

  const strikeOwn = (tone: StrikeTone): void => {
    if (openIsSpawned()) return;
    ownStruck.set(tone, performance.now());
    strike(tone);
  };
  createEffect(
    on(chatBusy, (busy) => busy && strikeOwn("info"), { defer: true }),
  );
  createEffect(
    on(awaitingKind, (kind) => kind && strikeOwn(ACTION_TONES[kind].status), {
      defer: true,
    }),
  );

  // The open thread's own run counts before the list catches up with it.
  createEffect(() => {
    const rows = sessions() ?? [];
    const own = openIsSpawned()
      ? 0
      : (chatBusy() ? 1 : 0) + (awaitingInput() ? 1 : 0);
    setAmbientLoad(Math.max(activityLoad(rows), own));
    setAmbientAurora(liveSubagents(rows));
  });

  let previous: Parameters<typeof diffActivity>[0];
  createEffect(() => {
    const rows = sessions();
    if (!rows) return;
    const { strikes, snapshot } = diffActivity(previous, rows);
    previous = snapshot;
    const now = performance.now();
    const open = chatThreadId();
    for (const { threadId, tone } of strikes) {
      const own = ownStruck.get(tone);
      if (threadId === open && own !== undefined && now - own < OWN_STRIKE_MS) {
        // The list catching up with a strike the open thread already made.
        ownStruck.delete(tone);
        continue;
      }
      strike(tone);
    }
  });

  createEffect(
    on(
      () => notifications.lastCreated,
      (n) => {
        if (n && BELL_KINDS.has(n.kind)) strike("neutral");
      },
      { defer: true },
    ),
  );
}
