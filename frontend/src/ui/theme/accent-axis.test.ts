import { describe, expect, test } from "bun:test";
import { createAccentAxis, readTheme, writeTheme } from "./accent-axis";
import type { AccentOverrides } from "./accent-overrides";

/**
 * The accent store's rules — read-back, clear-on-default and the prune — tested over an
 * in-memory handle, so none of this needs a `localStorage` or a `document`.
 */

const BASE = { shipped: () => "#34d67f" };

/** An in-memory stand-in for `accent-overrides`' signal + `update`. */
function harness() {
  let held: AccentOverrides = {};
  return {
    handle: {
      read: () => held,
      update: (next: AccentOverrides) => {
        held = next;
      },
    },
    get: () => held,
  };
}

describe("an override reads back", () => {
  test("the shipped value until something is set", () => {
    const h = harness();
    const axis = createAccentAxis(BASE, h.handle);
    expect(axis.value("phosphor", "accent")).toBe("#34d67f");
    expect(axis.isOverridden("phosphor", "accent")).toBe(false);
    axis.set("phosphor", "accent", "#112233");
    expect(axis.value("phosphor", "accent")).toBe("#112233");
    expect(axis.isOverridden("phosphor", "accent")).toBe(true);
  });

  test("a value that is not a colour is refused outright", () => {
    const h = harness();
    createAccentAxis(BASE, h.handle).set(
      "phosphor",
      "accent",
      "#000;}html{display:none",
    );
    expect(h.get()).toEqual({});
  });
});

describe("clearing leaves nothing behind — the reason it is written once", () => {
  test("resetting prunes the theme it emptied", () => {
    // Not cosmetic: `hasAccentOverrides` answers by counting keys, so an empty map
    // left behind keeps the RESET ALL control on screen with nothing to reset.
    const h = harness();
    const axis = createAccentAxis(BASE, h.handle);
    axis.set("phosphor", "accent", "#112233");
    axis.reset("phosphor", "accent");
    expect(h.get()).toEqual({});
  });

  test("a sibling in the same theme survives the prune", () => {
    const h = harness();
    const axis = createAccentAxis(BASE, h.handle);
    axis.set("phosphor", "accent", "#112233");
    axis.set("phosphor", "accent-warn", "#445566");
    axis.reset("phosphor", "accent");
    expect(h.get()).toEqual({ phosphor: { "accent-warn": "#445566" } });
  });

  test("setting a key back to its shipped value clears rather than stores it", () => {
    // Otherwise "I set it back by hand" and "I pressed reset" leave different
    // states, and the reset control lingers beside a key overriding nothing.
    const h = harness();
    const axis = createAccentAxis(BASE, h.handle);
    axis.set("phosphor", "accent", "#112233");
    axis.set("phosphor", "accent", "#34D67F"); // the shipped value, shouted
    expect(h.get()).toEqual({});
    expect(axis.isOverridden("phosphor", "accent")).toBe(false);
  });
});

describe("the walk survives whatever localStorage holds", () => {
  test("a blob of the wrong shape reads as nothing", () => {
    expect(readTheme("nope", "phosphor")).toBeUndefined();
    expect(readTheme(null, "phosphor")).toBeUndefined();
    expect(readTheme({ phosphor: 7 }, "phosphor")).toBeUndefined();
  });

  test("writing an empty entry drops the theme", () => {
    expect(writeTheme({ paper: { accent: "#112233" } }, "paper", {})).toEqual(
      {},
    );
  });
});
