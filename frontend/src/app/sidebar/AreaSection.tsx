import { For, Show, type Accessor, type JSX } from "solid-js";
import { useLocation } from "@solidjs/router";
import { DisclosureToggle, ListGroupHeader, ListRow, Tooltip } from "~/ui";
import type { NavArea } from "../nav";
import { areaMeta, navMeta } from "./navMeta";

/** One area of the rail's footer: a plate naming it (`COMMS ───── −`), and the area's
 *  pages beneath it as ruled destination rows while it is open.
 *
 *  The plate is a heading, not a link: the pages under it are the destinations, and
 *  the −/+ beside it opens and closes them without navigating, so an area can be
 *  peeked at without leaving the current page. While it is shut the plate carries the
 *  area's strongest indicator, or its activity — a parked approval included — would go
 *  dark the moment the operator looked away. */
export function AreaSection(props: {
  area: NavArea;
  active: boolean;
  open: Accessor<boolean>;
  onToggle: () => void;
}): JSX.Element {
  const location = useLocation();
  const isActive = (href: string) =>
    location.pathname === href || location.pathname.startsWith(`${href}/`);

  return (
    <div class="border-t border-line">
      <ListGroupHeader
        variant="plate"
        label={props.area.label}
        class="pt-2.5 pb-1.5"
        aside={
          <>
            <Show when={!props.open()}>{areaMeta(props.area)}</Show>
            <DisclosureToggle
              open={props.open()}
              onToggle={props.onToggle}
              label={props.area.label}
            />
          </>
        }
      />
      <Show when={props.open()}>
        <For each={props.area.items}>
          {(item) => (
            <Tooltip
              delay={1000}
              side="right"
              label={item.description}
              class="block w-full"
            >
              <ListRow
                variant="nav"
                label={item.label}
                leading={item.icon}
                href={item.href}
                selected={isActive(item.href)}
                right={navMeta(item)}
                // Indented so the rows read as the plate's contents.
                class="pl-6"
              />
            </Tooltip>
          )}
        </For>
      </Show>
    </div>
  );
}
