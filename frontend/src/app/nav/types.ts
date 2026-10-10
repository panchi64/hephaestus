import type { IconName, Status } from "~/ui";

/** Ambient activity on a nav item (unread mail, a degraded service). The shared
 *  `Status` union, not a nav-local one, so the state→accent mapping can't fork. */
export type NavIndicator = Status;

export interface NavItem {
  label: string;
  href: string;
  icon: IconName;
  /** One-line capability description. Hover tooltip on the row, subtitle in the
   *  switcher, and matched by search — so a surface hidden behind its area is
   *  still findable by what it does. */
  description: string;
  indicator?: NavIndicator;
  /** Whether this surface is wired to the backend. Unconnected surfaces are
   *  marked in the rail and overlaid with a NOT CONNECTED banner. */
  connected?: boolean;
}

/** A group of pages. Every area's header is always visible in the rail; which
 *  section is expanded follows the route — derived, never stored — so a deep
 *  link, the back button, and a search jump all reconcile the rail on their
 *  own. */
export interface NavArea {
  id: string;
  /** Switcher label, uppercase. */
  label: string;
  icon: IconName;
  description: string;
  items: NavItem[];
}

/** A destination kept outside the switcher, always one click away. A pin is a
 *  single page, not a group — that is why it isn't an area with one item: the
 *  switcher lists groups, and a lone page in that list has nothing to switch to. */
/**
 * One row the rail draws, and where.
 *
 * A row is *placement only*. It says nothing about whether a surface exists —
 * that is what the surface lists themselves say, and the two are deliberately
 * separate declarations. Deleting a row removes a row and nothing else.
 *
 * It carries the surface rather than an href, so a row pointing at a page that
 * does not exist cannot be written down.
 */
export interface RailRow {
  /** `top` above the switcher, `footer` above COMMS. */
  slot: "top" | "footer";
  /** The surface this row opens. It may be one an area already owns, in which
   *  case the area still owns it and the row is only a shortcut. */
  item: NavItem;
}

/** An item paired with the area that owns it — the unit route resolution returns.
 *  A pin with no area of its own (Chat) matches with `area` undefined. */
export interface NavMatch {
  item: NavItem;
  area?: NavArea;
}
