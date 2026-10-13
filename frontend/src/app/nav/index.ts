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

/**
 * Routes whose content column is backed by the deep field (§11.1).
 *
 * The field is the *page's* ground, not a region's — cropped to one band it loses the
 * limb, and a horizon you cannot see run the width of the frame is not a horizon. So
 * the shell hosts it behind the whole column (status bar included) rather than letting
 * a screen paint one inside its own padding box, and the screen stops knowing about it.
 *
 * It stays rationed. Two surfaces qualify: the launchpad, whose focal object is a
 * composer and whose other panels are readouts that go glass over it, and the 404,
 * which has one plate on it and nothing running. A route earns a place here only if
 * nothing on it is read *against* the field — everything with a surface frosts it out
 * (`.heph-framed`, theme.css), and everything else on the page is a label or a number
 * with space around it.
 *
 * A list rather than a flag on `NavItem`, for the same reason as `FLUSH_TOP_ROUTES`
 * above: this is the shell deciding what it paints behind its region.
 */
const DEEP_FIELD_ROUTES = ["/"];

/** Whether the shell paints the deep field behind this route's column.
 *
 *  **Exact, not `matchesHref`** — and not because a root href would over-match: it
 *  would not. `matchesHref(p, "/")` asks `p === "/" || p.startsWith("//")`, and no
 *  path starts with a double slash, so `/` claims nothing but itself either way. The
 *  reason is what the list *means*. `FLUSH_TOP_ROUTES` hands a region to a surface,
 *  and whatever hangs off that surface is still it, so a child must inherit. This
 *  list is a ration, and a route earns a place on it by having nothing that is read
 *  against the field — which is a fact about one screen and never inherited. Under
 *  `matchesHref` an entry like `/docs` would silently extend the field to every
 *  `/docs/*` detail page, each of them full of content and none of them reviewed.
 *
 *  `routes` is injectable for the same reason `areas` is on the resolvers above: the
 *  rule has to be testable against a fixture that fights it, and the real list holds
 *  only `/`, where exact and prefix matching agree. */
export function isDeepFieldRoute(
  pathname: string,
  routes: string[] = DEEP_FIELD_ROUTES,
): boolean {
  return routes.includes(pathname);
}
