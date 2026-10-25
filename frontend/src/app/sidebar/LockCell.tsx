import { Show, type JSX } from "solid-js";
import { Frames, Icon, StatusCell, toast } from "~/ui";
import { createInFlight } from "~/lib/inFlight";
import { useSession } from "~/lib/stores/session";

/** LOCK, as the rail footer's trailing cell.
 *
 *  The call is a round trip, so the cell shows a throbber across it, and a failure is
 *  said out loud: "it didn't lock" is the one outcome no calm-looking screen may hide.
 *  It has no keyboard shortcut, on purpose: ending the session is never a chord away. */
export function LockCell(): JSX.Element {
  const session = useSession();
  const locking = createInFlight<"lock">();
  const onLock = (): void => {
    void locking.run("lock", async () => {
      try {
        await session.lock();
      } catch (err) {
        // This session ended either way, which is why the screen still goes to the
        // gate — but the vault key is the backend's, and it never said it dropped it.
        toast.error(
          `This session ended, but the vault did NOT lock — the backend never confirmed it. ${
            err instanceof Error ? err.message : "Try again."
          }`,
        );
      }
    });
  };
  return (
    <StatusCell
      disabled={locking.has("lock")}
      onClick={onLock}
      class="border-l border-line px-3"
    >
      <Show
        when={locking.has("lock")}
        fallback={<Icon name="lock" size={16} />}
      >
        <Frames />
      </Show>
      {locking.has("lock") ? "locking…" : "lock"}
    </StatusCell>
  );
}
