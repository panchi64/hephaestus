import { createSignal, For, type JSX } from "solid-js";
import { useLocation } from "@solidjs/router";
import { Icon, ListRow, StatusBar, StatusCell, Tooltip } from "~/ui";
import { useSettingsRoute } from "~/app/settings-dialog";
import { RecentsRail } from "~/features/chat/components/RecentsRail";
import { AREAS, areaForPath, railRows, type NavItem } from "../nav";
import { AreaSection } from "./AreaSection";
import { LockCell } from "./LockCell";
import { navMeta } from "./navMeta";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { RailFrame } from "./RailFrame";

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

/** The rail: the conversation list as its body, with the few true destinations in the
 *  footer. Configuration lives in the settings dialog, not here.
 *
 *  COMMS sits in the footer because mail and a calendar are things you check, not the
 *  thing you are doing. It is collapsed unless the route is inside it. */
export function Sidebar(): JSX.Element {
  const location = useLocation();
  const settings = useSettingsRoute();
  const activeArea = () => areaForPath(location.pathname);
  const isActive = (href: string) =>
    location.pathname === href || location.pathname.startsWith(`${href}/`);

  const comms = AREAS[0];
  // Open when the route is inside COMMS, otherwise whatever the operator last toggled.
  const [commsOverride, setCommsOverride] = createSignal<boolean>();
  const commsOpen = (): boolean =>
    commsOverride() ?? activeArea()?.id === comms.id;

  return (
    <RailFrame
      brandHref="/"
      assetId="heph-workspace-02.1"
      footer={
        <>
          {/* The corpus and the compare bench sit here for the same reason as COMMS:
              above the threads they would compete with chat for the top of the rail. */}
          <For each={railRows("footer")}>
            {(item) => <PinRow item={item} active={isActive(item.href)} />}
          </For>

          <AreaSection
            area={comms}
            active={activeArea()?.id === comms.id}
            open={commsOpen}
            onToggle={() => setCommsOverride(!commsOpen())}
          />

          {/* The two things you do *to* the workspace rather than in it. Settings
              opens a dialog and changes no route, so it is a button, not a
              destination row. */}
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
            end={<LockCell />}
          />
        </>
      }
    >
      {/* Mounted on every route, not only `/chat`, so the conversation list stays put
          while you look at anything else. */}
      <RecentsRail filter={<ProjectSwitcher />} />
    </RailFrame>
  );
}
