/**
 * The viewport's own persisted state — presentation-only localStorage seams the panel and
 * its controls read and write. Nothing here is authoritative: it is the operator's
 * last-seen arrangement and reading preferences, never a business decision.
 *
 * **The layout is the open state.** v3 stored an `open` boolean beside the pinned
 * version, which was enough when the panel held one thing and "open" meant "showing it".
 * A panel that holds a set needs to remember *which* set, and once it does, `open` is a
 * second source of truth for something the layout already answers — and one that makes
 * "open with nothing in it" reachable. So openness is derived, and `lastLayout` is what
 * the toggle restores, because a panel that reopens empty has forgotten the thing the
 * operator was looking at.
 *
 * **What belongs to a surface now lives with it.** `pinnedKey`, `activeTab` and the seen
 * pointer were top-level fields only because there was one surface to own them. They are
 * the View's, and they move into its own bag; `fontStep` and `softWrap` stay shared,
 * because text set larger in one pane and not its neighbour reads as a bug rather than a
 * preference.
 *
 * Width is not here: it stopped being a preference the moment it became arithmetic over
 * the open set, and lives in `viewportWidth.ts`.
 *
 * **Reading is total, and never writes.** The record is JSON in localStorage, so it can
 * be absent, stale, hand-edited or written by a version that knew different surfaces.
 * Every read validates and prunes — an unknown surface id is dropped rather than handed
 * to a renderer that does not exist. Seeding from v3 follows the discipline v3 itself
 * kept with v2: the older key is read and never rewritten, so rolling back loses nothing.
 */

import { createSignal } from "solid-js";
import { readLS, writeLS } from "~/lib/storage";
import { isSurfaceId, leaf, pruneLayout, type ViewportLayout } from "./layout";
import type { PaneNode } from "./paneTree";
import type { SurfaceId } from "./surfaces";

/** The View surface's own preferences. */
export interface ViewSurfaceState {
  /** null = follow the newest item; otherwise the pinned item's key. */
  pinnedKey: string | null;
  activeTab: "preview" | "code";
}

/** Per-surface bags. Each surface adds its own optional field when it has state of
 *  its own; a surface with none never appears here. */
export interface SurfaceBags {
  view?: ViewSurfaceState;
}

export interface ViewportPersistedState {
  /** What is on screen. `null` is the panel being closed — see the module note. */
  layout: ViewportLayout | null;
  /** What reopening restores. Kept when the panel closes, so the toggle brings back
   *  the arrangement rather than a default one. */
  lastLayout: ViewportLayout | null;
  focused: SurfaceId | null;
  fullscreen: boolean;
  /** -2..+2, 0 = default size. Shared by every surface that renders text. */
  fontStep: number;
  softWrap: boolean;
  surfaces: SurfaceBags;
  /** Per surface, the key of the newest item the operator has seen. The header badge
   *  counts items after this key's *position* in the surface's current list — a
   *  "seen through" pointer rather than a raw count, so it self-corrects when a list
   *  shrinks (a rewind) and later regrows past a stale count. A key no longer in the
   *  list resolves to "nothing seen". */
  seen: Partial<Record<SurfaceId, string>>;
}

export const DEFAULT_VIEW_SURFACE: ViewSurfaceState = {
  pinnedKey: null,
  activeTab: "preview",
};

const DEFAULT_STATE: ViewportPersistedState = {
  layout: null,
  lastLayout: null,
  focused: null,
  fullscreen: false,
  fontStep: 0,
  // On by default: an unwrapped split diff puts the whole file behind a
  // horizontal scroll and lets long lines run across the column divider.
  softWrap: true,
  surfaces: {},
  seen: {},
};

const V4_KEY = "heph.chat.viewer.v4";
/** The prior per-conversation record: one surface, an `open` flag, and the View's
 *  preferences at the top level. Read once to seed a conversation's first v4 entry,
 *  and never written again. */
const V3_KEY = "heph.chat.viewer.v3";
/** Read only to seed v3, which v3 itself did — kept so a conversation that has sat
 *  untouched across both migrations still arrives with its preferences. */
const V2_KEY = "heph.chat.viewer.v2";
/** Legacy per-conversation open-state map (`ChatRoomScreen`'s prior `VIEWPORT_KEY`). */
const LEGACY_OPEN_KEY = "heph.chat.viewport";

// ── v3, as it was ────────────────────────────────────────────────────────────

interface V3State {
  open: boolean;
  pinnedKey: string | null;
  activeTab: "preview" | "code";
  fontStep: number;
  softWrap: boolean;
  fullscreen: boolean;
  seenKey: string | null;
}

const DEFAULT_V3: V3State = {
  open: false,
  pinnedKey: null,
  activeTab: "preview",
  fontStep: 0,
  softWrap: true,
  fullscreen: false,
  seenKey: null,
};

function readJson<T>(key: string): T | null {
  const raw = readLS(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/** A conversation's v3 record, itself seeded from v2 or the legacy open map — the
 *  chain v3 already implemented, preserved so a long-dormant thread migrates the
 *  whole way rather than only from the most recent format. */
function seedV3(conversationId: string): V3State {
  const v3 = readJson<Record<string, V3State>>(V3_KEY)?.[conversationId];
  if (v3) return { ...DEFAULT_V3, ...v3 };
  const v2 = readJson<Record<string, V3State>>(V2_KEY)?.[conversationId];
  // v2 is identical in shape but was written while `softWrap` defaulted *off*, so
  // every other preference carries over and only that one is re-defaulted.
  if (v2) return { ...DEFAULT_V3, ...v2, softWrap: DEFAULT_V3.softWrap };
  const legacyOpen =
    readJson<Record<string, boolean>>(LEGACY_OPEN_KEY)?.[conversationId] ??
    false;
  return { ...DEFAULT_V3, open: legacyOpen };
}

/** v3 → v4. The single surface it described was always the View. */
function fromV3(v3: V3State): ViewportPersistedState {
  const viewLayout: ViewportLayout = { strips: [], panels: leaf("view") };
  return {
    layout: v3.open ? viewLayout : null,
    // Even a thread that was closed gets a restore point, so its first open lands on
    // the View rather than on nothing.
    lastLayout: viewLayout,
    focused: v3.open ? "view" : null,
    fullscreen: v3.fullscreen,
    fontStep: v3.fontStep,
    softWrap: v3.softWrap,
    surfaces: {
      view: { pinnedKey: v3.pinnedKey, activeTab: v3.activeTab },
    },
    seen: v3.seenKey !== null ? { view: v3.seenKey } : {},
  };
}

// ── Validation ───────────────────────────────────────────────────────────────

/** Structural check on a persisted pane. The record is JSON the operator can edit
 *  and a past version may have written differently, so a malformed node is rejected
 *  here rather than crashing a renderer that assumed the shape. */
function parsePane(value: unknown): PaneNode<SurfaceId> | null {
  if (typeof value !== "object" || value === null) return null;
  const node = value as Record<string, unknown>;
  if (node.kind === "leaf") {
    return typeof node.surface === "string" && isSurfaceId(node.surface)
      ? leaf(node.surface)
      : null;
  }
  if (node.kind === "stack") {
    if (!Array.isArray(node.surfaces)) return null;
    // Deduped: a stack is a set of tabs, and the same surface twice would be two
    // tabs onto one pane that close together — `closeSurface` drops every match.
    const surfaces = [
      ...new Set(
        node.surfaces.filter(
          (s): s is SurfaceId => typeof s === "string" && isSurfaceId(s),
        ),
      ),
    ];
    if (surfaces.length === 0) return null;
    if (surfaces.length === 1) return leaf(surfaces[0]);
    const active =
      typeof node.active === "string" &&
      isSurfaceId(node.active) &&
      surfaces.includes(node.active)
        ? node.active
        : surfaces[0];
    return { kind: "stack", surfaces, active };
  }
  if (node.kind === "split") {
    const a = parsePane(node.a);
    const b = parsePane(node.b);
    // A split that lost one side is not a broken layout — it is the surviving side.
    if (a === null) return b;
    if (b === null) return a;
    const ratio =
      typeof node.ratio === "number" && node.ratio > 0 && node.ratio < 1
        ? node.ratio
        : 0.5;
    return {
      kind: "split",
      dir: node.dir === "row" ? "row" : "col",
      ratio,
      a,
      b,
    };
  }
  return null;
}

function parseLayout(value: unknown): ViewportLayout | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  const strips = Array.isArray(raw.strips)
    ? raw.strips.filter(
        (s): s is SurfaceId => typeof s === "string" && isSurfaceId(s),
      )
    : [];
  const panels = parsePane(raw.panels);
  const layout = pruneLayout({ strips, panels });
  // An arrangement that pruned away to nothing is the same as not having one.
  return layout.strips.length === 0 && layout.panels === null ? null : layout;
}

/** Coerce one stored record into a well-formed state, filling anything missing from
 *  the defaults. Total by construction — a record it cannot make sense of comes back
 *  as the defaults rather than as an exception. */
/** The View's bag, field by field. Spreading whatever was stored would let a
 *  hand-edited record put a string where `activeTab` goes and hand it straight to
 *  a tab strip — the one thing a validator that calls itself total must not do. */
function parseViewBag(value: unknown): ViewSurfaceState | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const raw = value as Record<string, unknown>;
  return {
    pinnedKey: typeof raw.pinnedKey === "string" ? raw.pinnedKey : null,
    activeTab: raw.activeTab === "code" ? "code" : "preview",
  };
}

function parseState(value: unknown): ViewportPersistedState {
  if (typeof value !== "object" || value === null) return DEFAULT_STATE;
  const raw = value as Record<string, unknown>;
  const focused =
    typeof raw.focused === "string" && isSurfaceId(raw.focused)
      ? raw.focused
      : null;
  const layout = parseLayout(raw.layout);
  const seen: Partial<Record<SurfaceId, string>> = {};
  if (typeof raw.seen === "object" && raw.seen !== null) {
    for (const [id, key] of Object.entries(raw.seen)) {
      if (isSurfaceId(id) && typeof key === "string") seen[id] = key;
    }
  }
  const bags =
    typeof raw.surfaces === "object" && raw.surfaces !== null
      ? (raw.surfaces as Record<string, unknown>)
      : {};
  return {
    layout,
    lastLayout: parseLayout(raw.lastLayout),
    // A focused surface that is no longer on screen is not focused.
    focused:
      layout !== null && focused !== null && hasId(layout, focused)
        ? focused
        : null,
    fullscreen: raw.fullscreen === true,
    fontStep:
      typeof raw.fontStep === "number" ? raw.fontStep : DEFAULT_STATE.fontStep,
    softWrap:
      typeof raw.softWrap === "boolean" ? raw.softWrap : DEFAULT_STATE.softWrap,
    surfaces: { view: parseViewBag(bags.view) },
    seen,
  };
}

function hasId(layout: ViewportLayout, id: SurfaceId): boolean {
  return (
    layout.strips.includes(id) ||
    (layout.panels !== null && paneHas(layout.panels, id))
  );
}

function paneHas(node: PaneNode<SurfaceId>, id: SurfaceId): boolean {
  switch (node.kind) {
    case "leaf":
      return node.surface === id;
    case "stack":
      return node.surfaces.includes(id);
    case "split":
      return paneHas(node.a, id) || paneHas(node.b, id);
  }
}

// ── The store ────────────────────────────────────────────────────────────────

type PersistedMap = Record<string, unknown>;

function readV4Map(): PersistedMap {
  return readJson<PersistedMap>(V4_KEY) ?? {};
}

/** A conversation's state: its v4 record when it has one, else migrated from v3.
 *  Pure — reading never writes, so the seed is re-derived until the first `patch`
 *  and a read cannot corrupt an older record. */
function seedState(
  conversationId: string,
  map: PersistedMap,
): ViewportPersistedState {
  const existing = map[conversationId];
  if (existing !== undefined) return parseState(existing);
  return fromV3(seedV3(conversationId));
}

// Module-level so the panel's state survives the screen remounting on navigation.
const [v4Map, setV4Map] = createSignal<PersistedMap>(readV4Map());

/** Per-conversation viewport state, backed by localStorage. `state()` is reactive
 *  (tracks the module-level store); `patch` merges and persists immediately. */
export function useViewportPersistence(conversationId: () => string): {
  state: () => ViewportPersistedState;
  patch: (p: Partial<ViewportPersistedState>) => void;
} {
  const state = (): ViewportPersistedState =>
    seedState(conversationId(), v4Map());
  const patch = (p: Partial<ViewportPersistedState>): void => {
    const id = conversationId();
    const next: ViewportPersistedState = { ...seedState(id, v4Map()), ...p };
    const nextMap = { ...v4Map(), [id]: next };
    setV4Map(nextMap);
    writeLS(V4_KEY, JSON.stringify(nextMap));
  };
  return { state, patch };
}

/** Test seam: drop the in-memory copy so the next read re-seeds from storage. */
export function resetViewportPersistence(): void {
  setV4Map(readV4Map());
}
