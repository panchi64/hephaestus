import { AREAS, LOOSE_SURFACES, RAIL_ROWS } from "./areas";
import type { NavArea, NavItem, NavMatch } from "./types";

export { AREAS, LOOSE_SURFACES, RAIL_ROWS };
export type {
  NavArea,
  NavIndicator,
  NavItem,
  NavMatch,
  RailRow,
} from "./types";

/** The rail's rows for one slot, in declaration order. Placement only — a
 *  surface missing from every slot still exists (see `areas.ts`). */
export const railRows = (slot: "top" | "footer"): NavItem[] =>
  RAIL_ROWS.filter((r) => r.slot === slot).map((r) => r.item);

/** Every page in the nav, area-owned or not — the map the shell and the titles
 *  read. A loose surface an area already owns is skipped: its page is here under
 *  its own label, and listing it twice would give a route two answers.
 *
 *  Note this reads the *surfaces*, never the rail rows. A page's existence does
 *  not depend on the rail drawing it. */
export function flattenNav(areas: NavArea[] = AREAS): NavMatch[] {
  const owned = areas.flatMap((area) =>
    area.items.map((item) => ({ item, area })),
  );
  const hrefs = new Set(owned.map((m) => m.item.href));
  return [
    ...owned,
    ...LOOSE_SURFACES.filter((item) => !hrefs.has(item.href)).map((item) => ({
      item,
    })),
  ];
}

function matchesHref(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Longest match wins — without that rule `/admin/access-tokens` resolves
 *  through `/admin/tokens`, and every detail route lands on whichever item
 *  happened to be declared first. */
function longestMatch(
  pathname: string,
  areas: NavArea[],
): NavMatch | undefined {
  let best: NavMatch | undefined;
  let bestLen = -1;
  for (const match of flattenNav(areas)) {
    if (
      matchesHref(pathname, match.item.href) &&
      match.item.href.length > bestLen
    ) {
      best = match;
      bestLen = match.item.href.length;
    }
  }
  return best;
}

/** The area a route belongs to, or `undefined` when no area owns it — `/`, a
 *  pinned page like `/chat`, and any unlisted route. The caller renders a neutral
 *  state rather than guessing: naming an area the operator isn't in is worse than
 *  naming none. */
export function areaForPath(
  pathname: string,
  areas: NavArea[] = AREAS,
): NavArea | undefined {
  const match = longestMatch(pathname, areas);
  if (!match && pathname !== "/" && import.meta.env.DEV) {
    console.warn(`[nav] no area claims "${pathname}"`);
  }
  return match?.area;
}

/** The item a route sits on or under, so a detail route highlights its parent. */
export function itemForPath(
  pathname: string,
  areas: NavArea[] = AREAS,
): NavItem | undefined {
  return longestMatch(pathname, areas)?.item;
}

/** Whether a route is backed by the real backend. Drives the NOT CONNECTED
 *  overlay. `/` has no nav entry but is connected; a parent of connected
 *  children counts too. */
export function isConnectedRoute(
  pathname: string,
  areas: NavArea[] = AREAS,
): boolean {
  if (pathname === "/") return true;
  return flattenNav(areas).some(
    ({ item }) =>
      item.connected &&
      (matchesHref(pathname, item.href) ||
        item.href.startsWith(`${pathname}/`)),
  );
}

/**
 * Routes handed the content region flush at the top and bottom, rather than inset like
 * a page.
 *
 * Nearly every surface here is a **document**: it scrolls inside the shell's `main`,
 * wants an even margin around it, and its `PageHeader` registration marks frame the
 * header row against that margin (§9). The chat room is not. It is a full-height
 * application surface — its own header, its own scroll container, a composer docked to
 * the bottom — so it already spaces itself, and the shell's top inset lands as empty
 * ground above a header that needed none, pushing the transcript down by a row of text
 * on every screen size. The bottom inset went the same way: under a docked composer it
 * was a band of dead page between the input and the window's edge, and the room keeps
 * the few pixels its composer's corner ticks need itself. The sides still hold the
 * column off the rail.
 *
 * A list rather than a flag on `NavItem`: this is the shell deciding how to hand over
 * its region, which is not something a nav entry knows or should carry.
 */
const FLUSH_ROUTES = ["/chat"];

/** Whether this route frames itself and should not be inset at the top or bottom. */
export function isFlushRoute(pathname: string): boolean {
  return FLUSH_ROUTES.some((href) => matchesHref(pathname, href));
}
