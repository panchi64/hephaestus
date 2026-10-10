import { createSignal, For, type JSX } from "solid-js";
import { useLocation } from "@solidjs/router";
import { Button, ListRow, Text, Tooltip, cx, toast } from "~/ui";
import { createInFlight } from "~/lib/inFlight";
import { useSession } from "~/lib/stores/session";
import { useSettingsRoute } from "~/app/settings-dialog";
import { RecentsRail } from "~/features/chat/components/RecentsRail";
import { SessionModeSwitch } from "~/features/chat/components/SessionModeSwitch";
import { AREAS, areaForPath, railRows, type NavItem } from "../nav";
import { AreaSection } from "./AreaSection";
import { navMeta } from "./navMeta";
import { ProjectSwitcher } from "./ProjectSwitcher";

/** A destination kept outside the one area. */
function PinRow(props: { item: NavItem; active: boolean }): JSX.Element {
  return (
    <Tooltip
      delay={1000}
      side="right"
      label={props.item.description}
      class="block w-full"
    >
      <ListRow
        label={props.item.label}
        leading={props.item.icon}
        href={props.item.href}
        selected={props.active}
        right={navMeta(props.item)}
        // Selection is a raised fill on a smoothed corner (§10 States), not the
        // old 2px left rule — a rail of vertical bars was reading as chrome, and
        // the fill says "you are here" without adding a line to the page.
        class={cx("rounded-ctl", props.active && "bg-raised")}
      />
    </Tooltip>
  );
}

/** The rail: the places you work, then the threads you work in.
 *
 *  It used to list nineteen surfaces across six areas — the app's table of
 *  contents, permanently occupying the space beside the work. Everything that
 *  was configuration now lives in the settings dialog, so what is left is the
 *  handful of surfaces that are genuinely destinations, and beneath them the
 *  conversation list, which is the rail's actual body.
 *
 *  COMMS is the one group left, and it sits in the footer rather than above the
 *  threads: mail and a calendar are things you check, not the thing you are
 *  doing. It is collapsed unless the route is inside it, so at rest the rail
 *  reads as chat with two rows underneath. */
export function Sidebar(): JSX.Element {
  const location = useLocation();
  const session = useSession();
  const settings = useSettingsRoute();
  const activeArea = () => areaForPath(location.pathname);
  const isActive = (href: string) =>
    location.pathname === href || location.pathname.startsWith(`${href}/`);

  const comms = AREAS[0];
  // Open when the route is inside COMMS, and otherwise whatever the operator
  // last toggled it to. Derived-then-overridden, like the area sections were —
  // arriving in the area always shows its pages.
  const [commsOverride, setCommsOverride] = createSignal<boolean>();
  const commsOpen = (): boolean =>
    commsOverride() ?? activeArea()?.id === comms.id;

  /** LOCK, while the backend is still being told.
   *
   *  The call is a round trip and the screen used to do nothing at all across it: the
   *  button stayed live, nothing moved, and on a backend that hangs the operator gets
   *  up and leaves a workspace that still looks — and is — unlocked. The throbber is
   *  the minimum, and the failure is said out loud, because "it didn't lock" is the
   *  one outcome here that no amount of the interface looking calm can be allowed to
   *  hide. */
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
    <nav class="flex h-full min-h-0 flex-col bg-surface">
      <div class="sticky top-0 z-30 bg-surface">
        <div class="flex items-center justify-between gap-2 pr-2">
          <a
            href="/"
            class="mx-1 flex min-w-0 flex-1 flex-col gap-1 rounded-ctl px-2 py-3 transition-colors hover:bg-raised"
          >
            {/* A wordmark is neither the interface talking nor the machine
                reporting — it is a name. Sans with the display tracking reads
                as a logotype; the old uppercase read as one more HUD label. */}
            <Text variant="readout" tone="bright" class="tracking-tight">
              Odysseus
            </Text>
            <Text variant="micro" tone="dim">
              ODY-WORKSPACE-02.1
            </Text>
          </a>
        </div>

        {/* What kind of work you are looking at — and, since the Chat row went,
            the way into it. It sits directly under the wordmark because it is
            now the rail's first control rather than one of several: everything
            below it is the work it files.

            It was a dropdown in the composer, offered only while a thread was
            unsaved — which is where a property of the *message* would belong.
            The mode is a property of the thread, so it belongs to the list of
            threads: it files what the rail shows, decides what the next new
            thread is, and repaints the signature accent, which are one fact at
            three ranges. */}
        <SessionModeSwitch />
      </div>

      <ProjectSwitcher />

      {/* The threads. Mounted on every route, not only `/chat` — the rail used
          to host this through a route-matched slot, which made the conversation
          list disappear the moment you looked at anything else. */}
      <RecentsRail />

      <div class="sticky bottom-0 mt-2 bg-surface pt-2">
        {/* The corpus and the compare bench, on the same footing as COMMS and for
            the same reason: they are things you go and check, not the thing you
            are doing. Above the threads they read as peers of chat and competed
            with it for the top of the rail; down here they are a click away and
            the rail above is one subject. */}
        <For each={railRows("footer")}>
          {(item) => <PinRow item={item} active={isActive(item.href)} />}
        </For>

        <AreaSection
          area={comms}
          active={activeArea()?.id === comms.id}
          open={commsOpen}
          onToggle={() => setCommsOverride(!commsOpen())}
        />

        {/* The last line: the two things you do *to* the workspace rather than
            in it.

            `Operator` held the left of this strip and was a label, not a
            control — it named who was logged in, in a single-operator product
            where the answer never changes and nothing else asks. Settings was a
            row of its own directly above, looking like a destination while
            changing no route. Folding one into the other costs a row and gives
            the strip two controls that both act on the session.

            Settings still opens the dialog rather than navigating, which is why
            it is a button and not a `ListRow` with an `href`: a row that looks
            like every other destination but changes no route would be a lie
            about what clicking it does. */}
        <div class="flex items-center justify-between gap-2 px-1 py-2">
          <Tooltip
            delay={1000}
            side="right"
            label="Preferences, connections, and the state of the machine"
          >
            <Button
              variant="ghost"
              size="sm"
              leading="settings"
              active={settings.open()}
              onClick={() => settings.show()}
            >
              Settings
            </Button>
          </Tooltip>
          <Button
            variant="ghost"
            size="sm"
            leading="lock"
            pending={locking.has("lock")}
            onClick={onLock}
          >
            {locking.has("lock") ? "Locking…" : "Lock"}
          </Button>
        </div>
      </div>
    </nav>
  );
}
