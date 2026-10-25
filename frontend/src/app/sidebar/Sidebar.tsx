import { createSignal, For, Show, type JSX } from "solid-js";
import { useLocation } from "@solidjs/router";
import { pluginEnabled } from "~/lib/stores/builtinPlugins";
import { pluginOfHref } from "~/plugins/registry";
import {
  Frames,
  Icon,
  ListRow,
  StatusBar,
  StatusCell,
  Text,
  Tooltip,
  toast,
} from "~/ui";
import { createInFlight } from "~/lib/inFlight";
import { useSession } from "~/lib/stores/session";
import { useSettingsRoute } from "~/app/settings-dialog";
import { RecentsRail } from "~/features/chat/components/RecentsRail";
import {
  AREAS,
  areaForPath,
  railRows,
  type NavArea,
  type NavItem,
} from "../nav";
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
        variant="nav"
        label={props.item.label}
        leading={props.item.icon}
        href={props.item.href}
        selected={props.active}
        right={navMeta(props.item)}
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

  // A switched-off built-in plugin's pages drop out of the rail; the routes stay
  // registered and say the plugin is off.
  const comms = (): NavArea => ({
    ...AREAS[0],
    items: AREAS[0].items.filter((item) => {
      const plugin = pluginOfHref(item.href);
      return plugin === undefined || pluginEnabled(plugin);
    }),
  });
  // Open when the route is inside COMMS, and otherwise whatever the operator
  // last toggled it to. Derived-then-overridden, like the area sections were —
  // arriving in the area always shows its pages.
  const [commsOverride, setCommsOverride] = createSignal<boolean>();
  const commsOpen = (): boolean =>
    commsOverride() ?? activeArea()?.id === comms().id;

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
    <nav class="heph-rail heph-rail-glass flex h-full min-h-0 flex-col">
      <div class="sticky top-0 z-30">
        {/* The wordmark in the machine's voice, wide-tracked, with the workspace id
            dim beside it. It stacks under the brand at the rail's narrowest. */}
        <a href="/" class="heph-rail-brand hover:bg-raised">
          <span class="heph-rail-wordmark">Hephaestus</span>
          <Text variant="micro" tone="dim">
            heph-workspace-02.1
          </Text>
        </a>
      </div>

      {/* The threads, with the search strip over them and the project filter at
          the right end of their heading. Mounted on every route, not only `/chat` —
          the rail used to host this through a route-matched slot, which made the
          conversation list disappear the moment you looked at anything else. */}
      <RecentsRail filter={<ProjectSwitcher />} />

      <div class="sticky bottom-0 border-t border-line-strong">
        {/* The corpus and the compare bench, on the same footing as COMMS and for
            the same reason: they are things you go and check, not the thing you
            are doing. Above the threads they read as peers of chat and competed
            with it for the top of the rail; down here they are a click away and
            the rail above is one subject. */}
        <For each={railRows("footer")}>
          {(item) => <PinRow item={item} active={isActive(item.href)} />}
        </For>

        <AreaSection
          area={comms()}
          active={activeArea()?.id === comms().id}
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
            it is a button and not a destination row: a row that looks like every
            other destination but changes no route would be a lie about what
            clicking it does. It recedes, like the composer's strip — the rail's
            last line is the one read least. Lock has no keyboard shortcut, on
            purpose: ending the session is never a chord away. */}
        <StatusBar
          variant="recede"
          start={
            <Tooltip
              delay={1000}
              side="right"
              label="Preferences, connections, and the state of the machine"
              class="flex flex-1"
            >
              <StatusCell
                active={settings.open()}
                onClick={() => settings.show()}
                class="flex-1 px-3"
              >
                <Icon name="settings" size={16} />
                settings
              </StatusCell>
            </Tooltip>
          }
          end={
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
          }
        />
      </div>
    </nav>
  );
}
