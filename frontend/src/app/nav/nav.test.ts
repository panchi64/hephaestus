/**
 * The nav model's derivations. Everything here is pure — `areas` is injectable
 * on every function precisely so the rules can be exercised against a fixture
 * rather than against whatever the real rail happens to contain today.
 *
 * Two kinds of test live here, and the split is deliberate:
 *  - Behaviour, against `FIXTURE`, so a test fails when a rule changes rather
 *    than when a page is added.
 *  - Invariants, against the real `AREAS` / surface lists, because those are claims the
 *    rail's documentation makes about the actual data and nothing else checks.
 */
import { describe, expect, test } from "bun:test";
import {
  AREAS,
  LOOSE_SURFACES,
  RAIL_ROWS,
  areaForPath,
  flattenNav,
  isConnectedRoute,
  isFlushRoute,
  itemForPath,
} from "./index";
import type { NavArea } from "./types";

const item = (
  label: string,
  href: string,
  opts: { connected?: boolean } = {},
) => ({
  label,
  href,
  icon: "file" as const,
  description: `${label} description`,
  ...(opts.connected === undefined ? {} : { connected: opts.connected }),
});

/** Shaped so each rule is exercised by data that would break if the rule were
 *  dropped — a fixture that merely resembles the real rail passes either way.
 *  The real rail has no prefix pair at all today, so `/docs` + `/docs/drafts`
 *  exist only here. */
const FIXTURE: NavArea[] = [
  {
    id: "alpha",
    label: "ALPHA",
    icon: "file",
    description: "alpha area",
    items: [
      // Declared BEFORE its own parent, deliberately: declaration order and
      // longest-match disagree about /docs/drafts, so first-wins and last-wins
      // both resolve it differently than longest-wins does.
      item("Drafts", "/docs/drafts", { connected: true }),
      item("Docs", "/docs", { connected: true }),
      item("Mock", "/mock"),
    ],
  },
  {
    id: "beta",
    label: "BETA",
    icon: "file",
    description: "beta area",
    items: [
      item("Other", "/other", { connected: true }),
      item("Archive", "/archive", { connected: true }),
      // Nothing owns the bare /group segment, mirroring how /settings is only
      // ever a redirect into its first section. The parent-of-connected-children
      // branch is the ONLY thing that can resolve it.
      item("Section", "/group/section", { connected: true }),
    ],
  },
];

describe("areaForPath", () => {
  test("longest match wins, so a nested page keeps its own area", () => {
    expect(areaForPath("/docs", FIXTURE)?.id).toBe("alpha");
    expect(itemForPath("/docs/drafts", FIXTURE)?.href).toBe("/docs/drafts");
    // Without longest-match this lands on /docs, and the deeper page can never
    // be the active item.
    expect(itemForPath("/docs/drafts/2024", FIXTURE)?.href).toBe(
      "/docs/drafts",
    );
  });

  test("a detail route resolves to its parent page", () => {
    expect(itemForPath("/docs/abc123", FIXTURE)?.href).toBe("/docs");
  });

  test("matching is segment-bounded, not a bare prefix", () => {
    // /docs must not claim /docs-archive.
    expect(areaForPath("/docs-archive", FIXTURE)).toBeUndefined();
  });

  test("returns undefined rather than guessing an area", () => {
    expect(areaForPath("/", FIXTURE)).toBeUndefined();
    expect(areaForPath("/unlisted", FIXTURE)).toBeUndefined();
  });
});

describe("flattenNav", () => {
  test("keeps a loose surface no area owns and drops one that shortcuts into an area", () => {
    const hrefs = flattenNav(AREAS).map((m) => m.item.href);
    const areaOwned = new Set(AREAS.flatMap((a) => a.items.map((i) => i.href)));

    for (const surface of LOOSE_SURFACES) {
      const occurrences = hrefs.filter((h) => h === surface.href).length;
      expect(occurrences).toBe(1);
      // An area-owned surface appears once via its area (with an `area`); a
      // standalone one appears once on its own (without one).
      const match = flattenNav(AREAS).find((m) => m.item.href === surface.href);
      expect(match?.area === undefined).toBe(!areaOwned.has(surface.href));
    }
  });
});

describe("the map and the rail are separate", () => {
  /** The bug this separation exists for: the rail's rows and the app's list of
   *  surfaces were one declaration, so deleting Chat's row deleted `/chat` from
   *  the map — and `isConnectedRoute` then painted NOT CONNECTED over the whole
   *  of chat. Placement must never decide existence. */
  test("a surface with no rail row is still a connected route", () => {
    const drawn = new Set(RAIL_ROWS.map((r) => r.item.href));
    expect(drawn.has("/chat")).toBe(false);
    expect(isConnectedRoute("/chat")).toBe(true);
  });

  test("every rail row points at a surface that is on the map", () => {
    const mapped = new Set(flattenNav(AREAS).map((m) => m.item.href));
    for (const row of RAIL_ROWS) expect(mapped.has(row.item.href)).toBe(true);
  });

  test("every loose surface is reachable, drawn or not", () => {
    for (const surface of LOOSE_SURFACES) {
      expect(isConnectedRoute(surface.href)).toBe(Boolean(surface.connected));
    }
  });
});

describe("isFlushRoute", () => {
  test("the chat room frames itself", () => {
    expect(isFlushRoute("/chat")).toBe(true);
  });

  test("every other surface keeps the inset its registration marks sit against", () => {
    expect(isFlushRoute("/")).toBe(false);
    expect(isFlushRoute("/knowledge")).toBe(false);
    // Not a prefix match on the string: a route that merely *starts* with a flush
    // one's path is a different surface and must not inherit its framing.
    expect(isFlushRoute("/chatter")).toBe(false);
  });

  test("a child route of the chat room stays flush", () => {
    // Whatever hangs off it is still the chat room, so the framing has to follow —
    // which is why this asks `matchesHref` rather than comparing strings.
    expect(isFlushRoute("/chat/anything")).toBe(true);
  });
});

describe("isConnectedRoute", () => {
  test("the home route is connected despite having no nav entry", () => {
    expect(isConnectedRoute("/", FIXTURE)).toBe(true);
  });

  test("an unconnected page is not, and neither is an unlisted route", () => {
    expect(isConnectedRoute("/mock", FIXTURE)).toBe(false);
    expect(isConnectedRoute("/unlisted", FIXTURE)).toBe(false);
  });

  test("a parent of connected children counts, so a redirect doesn't flash the overlay", () => {
    // No item owns /group; only /group/section does. This passes solely on the
    // parent branch — /docs would not prove it, since /docs is an item itself.
    expect(isConnectedRoute("/group", FIXTURE)).toBe(true);
  });

  test("a surface that became a settings section is not a route", () => {
    // These have no nav item and no route: configuration is reached through the
    // dialog, so `/settings` and `/vault` are dead paths that read as dead.
    expect(AREAS.flatMap((a) => a.items).some((i) => i.href === "/settings")) //
      .toBe(false);
    expect(isConnectedRoute("/settings")).toBe(false);
    expect(isConnectedRoute("/vault")).toBe(false);
  });
});

describe("the real nav data", () => {
  test("an href appears in at most one area", () => {
    const seen = new Map<string, string>();
    const collisions: string[] = [];
    for (const area of AREAS) {
      for (const navItem of area.items) {
        const owner = seen.get(navItem.href);
        if (owner) collisions.push(`${navItem.href}: ${owner} and ${area.id}`);
        else seen.set(navItem.href, area.id);
      }
    }
    expect(collisions).toEqual([]);
  });

  test("every area and item is non-empty and describable", () => {
    expect(AREAS.length).toBeGreaterThan(0);
    for (const area of AREAS) {
      expect(area.items.length).toBeGreaterThan(0);
      for (const navItem of area.items) {
        expect(navItem.href.startsWith("/")).toBe(true);
        expect(navItem.label.length).toBeGreaterThan(0);
        expect(navItem.description.length).toBeGreaterThan(0);
      }
    }
  });

  test("every area resolves from its own first item's path", () => {
    for (const area of AREAS) {
      expect(areaForPath(area.items[0]!.href)?.id).toBe(area.id);
    }
  });

  test("every remaining nav surface is wired to the backend", () => {
    // Health was the one exception, and it is no longer a route: it is a section
    // of the settings dialog, which renders its own inline NOT CONNECTED overlay
    // (see HealthScreen). Nothing reachable from the rail is on fixtures, so
    // this list is empty — and if an unwired surface is ever added to the nav,
    // this is what says so.
    const unconnected = flattenNav()
      .filter((m) => !m.item.connected)
      .map((m) => m.item.href);
    expect(unconnected).toEqual([]);
  });
});
