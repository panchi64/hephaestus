/** The viewport panel's persisted state, and the migration into v4.
 *
 *  **Migration.** v3 described a panel that held one thing; v4 describes one that holds a
 *  set. A thread that has sat untouched must come across with its pin, tab, font, wrap and
 *  seen position intact, and the older keys must survive untouched so a rollback loses
 *  nothing. The seeding path is also re-derived on every read until the first write, so it
 *  has to be pure — a read that quietly rewrote storage would make the rollback promise
 *  false.
 *
 *  `localStorage` does not exist under `bun test`, so it is stubbed before the module is
 *  imported. That is deliberate over testing the pure helpers in isolation: the thing
 *  worth pinning is the path that actually runs, storage reads and all.
 */

import { describe, expect, test } from "bun:test";
import type { ViewportLayout } from "./layout";

class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
  raw(key: string): string | null {
    return this.getItem(key);
  }
}

const storage = new MemoryStorage();
(globalThis as { localStorage?: unknown }).localStorage = storage;

// Imported after the stub is in place — the module reads storage at module scope.
const { useViewportPersistence, resetViewportPersistence } =
  await import("./persistence");

const V4_KEY = "heph.chat.viewer.v4";
const V3_KEY = "heph.chat.viewer.v3";
const V2_KEY = "heph.chat.viewer.v2";
const LEGACY_KEY = "heph.chat.viewport";

const VIEW_LEAF: ViewportLayout = {
  strips: [],
  panels: { kind: "leaf", surface: "view" },
};

/** Read a conversation's state through the real hook. */
const read = (id: string) => useViewportPersistence(() => id).state();
const patchOf = (id: string) => useViewportPersistence(() => id).patch;

function seedStorage(entries: Record<string, unknown>): void {
  storage.clear();
  for (const [key, value] of Object.entries(entries)) {
    storage.setItem(key, JSON.stringify(value));
  }
  resetViewportPersistence();
}

describe("v3 → v4", () => {
  const v3Record = {
    open: true,
    pinnedKey: "snapshot-7",
    activeTab: "code",
    fontStep: 2,
    softWrap: false,
    fullscreen: true,
    seenKey: "snapshot-9",
  };

  test("an open thread carries its whole arrangement across", () => {
    seedStorage({ [V3_KEY]: { c1: v3Record } });
    const state = read("c1");

    expect(state.layout).toEqual(VIEW_LEAF);
    expect(state.focused).toBe("view");
    expect(state.fullscreen).toBe(true);
    expect(state.fontStep).toBe(2);
    expect(state.softWrap).toBe(false);
    // The View's own preferences moved into its bag rather than being dropped.
    expect(state.surfaces.view).toEqual({
      pinnedKey: "snapshot-7",
      activeTab: "code",
    });
    expect(state.seen.view).toBe("snapshot-9");
  });

  test("a closed thread has no layout but still has somewhere to reopen to", () => {
    seedStorage({ [V3_KEY]: { c1: { ...v3Record, open: false } } });
    const state = read("c1");

    expect(state.layout).toBeNull();
    expect(state.focused).toBeNull();
    // Without this, the first toggle would open onto nothing.
    expect(state.lastLayout).toEqual(VIEW_LEAF);
  });

  test("reading never rewrites the v3 key, so a rollback loses nothing", () => {
    const original = JSON.stringify({ c1: v3Record });
    seedStorage({ [V3_KEY]: { c1: v3Record } });
    read("c1");
    read("c1");
    expect(storage.raw(V3_KEY)).toBe(original);
    // And nothing was written under v4 either — seeding is pure.
    expect(storage.raw(V4_KEY)).toBeNull();
  });

  test("seeding is idempotent", () => {
    seedStorage({ [V3_KEY]: { c1: v3Record } });
    expect(read("c1")).toEqual(read("c1"));
  });

  test("a thread with no record at all gets the defaults", () => {
    seedStorage({});
    const state = read("unknown");
    expect(state.layout).toBeNull();
    expect(state.fontStep).toBe(0);
    expect(state.softWrap).toBe(true);
    // The migration hands every thread a View bag, defaults included, rather than
    // leaving it absent — `viewState()` would fall back to the same values either
    // way, so this pins which of the two it actually is.
    expect(state.surfaces.view).toEqual({
      pinnedKey: null,
      activeTab: "preview",
    });
    expect(state.seen).toEqual({});
  });
});

describe("the older formats still migrate the whole way", () => {
  test("v2 comes across with soft wrap re-defaulted", () => {
    // v2 is identical in shape but was written while softWrap defaulted off.
    seedStorage({
      [V2_KEY]: {
        c1: {
          open: true,
          pinnedKey: "snapshot-2",
          activeTab: "preview",
          fontStep: 1,
          softWrap: false,
          fullscreen: false,
          seenKey: null,
        },
      },
    });
    const state = read("c1");
    expect(state.layout).toEqual(VIEW_LEAF);
    expect(state.fontStep).toBe(1);
    expect(state.surfaces.view?.pinnedKey).toBe("snapshot-2");
    expect(state.softWrap).toBe(true);
  });

  test("the legacy open map still opens the panel", () => {
    seedStorage({ [LEGACY_KEY]: { c1: true, c2: false } });
    expect(read("c1").layout).toEqual(VIEW_LEAF);
    expect(read("c2").layout).toBeNull();
  });

  test("v3 wins over v2 for the same thread", () => {
    seedStorage({
      [V3_KEY]: { c1: { open: false, fontStep: 0 } },
      [V2_KEY]: { c1: { open: true, fontStep: 2 } },
    });
    expect(read("c1").layout).toBeNull();
  });
});

describe("reading a v4 record back", () => {
  test("a written record survives the round trip", () => {
    seedStorage({});
    patchOf("c1")({ fontStep: -1, fullscreen: true });
    expect(read("c1").fontStep).toBe(-1);
    expect(read("c1").fullscreen).toBe(true);
    // And it is actually in storage under the v4 key now.
    expect(storage.raw(V4_KEY)).toContain("c1");
  });

  test("an unknown surface is pruned rather than handed to a missing renderer", () => {
    seedStorage({
      [V4_KEY]: {
        c1: {
          layout: {
            strips: ["ghost"],
            panels: { kind: "leaf", surface: "ghost" },
          },
          focused: "ghost",
        },
      },
    });
    const state = read("c1");
    expect(state.layout).toBeNull();
    expect(state.focused).toBeNull();
  });

  test("a split that lost one side collapses to the survivor", () => {
    seedStorage({
      [V4_KEY]: {
        c1: {
          layout: {
            strips: [],
            panels: {
              kind: "split",
              dir: "col",
              ratio: 0.5,
              a: { kind: "leaf", surface: "view" },
              b: { kind: "leaf", surface: "ghost" },
            },
          },
        },
      },
    });
    expect(read("c1").layout).toEqual(VIEW_LEAF);
  });

  test("a malformed record falls back rather than throwing", () => {
    seedStorage({ [V4_KEY]: { c1: { layout: "nonsense", fontStep: "big" } } });
    const state = read("c1");
    expect(state.layout).toBeNull();
    expect(state.fontStep).toBe(0);
  });

  test("a stack naming one surface twice becomes one tab", () => {
    // Two tabs onto one pane would close together — `closeSurface` drops every
    // match — so the second is a tab that cannot be shut.
    seedStorage({
      [V4_KEY]: {
        c1: {
          layout: {
            strips: [],
            panels: {
              kind: "stack",
              surfaces: ["view", "view"],
              active: "view",
            },
          },
        },
      },
    });
    expect(read("c1").layout).toEqual(VIEW_LEAF);
  });

  test("a surface bag is read field by field, not spread", () => {
    // The record is JSON the operator can edit, and `activeTab` goes straight to
    // a tab strip: anything but the two it knows has to come back as the default.
    seedStorage({
      [V4_KEY]: {
        c1: { surfaces: { view: { pinnedKey: 7, activeTab: "sideways" } } },
      },
    });
    expect(read("c1").surfaces.view).toEqual({
      pinnedKey: null,
      activeTab: "preview",
    });
  });

  test("a surface bag that is not an object is no bag at all", () => {
    seedStorage({ [V4_KEY]: { c1: { surfaces: { view: "preview" } } } });
    expect(read("c1").surfaces.view).toBeUndefined();
  });

  test("a focused surface that is not on screen is not focused", () => {
    seedStorage({
      [V4_KEY]: { c1: { layout: null, focused: "view" } },
    });
    expect(read("c1").focused).toBeNull();
  });
});
