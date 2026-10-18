import { describe, expect, test } from "bun:test";
import {
  firstReaching,
  nextFollow,
  nextPinned,
  pickAnchor,
  roomFor,
} from "./transcriptScrollRules";

/** A view from y=100 to y=900. */
const TOP = 100;
const BOTTOM = 900;

describe("pickAnchor", () => {
  test("the live answer wins while any of it is visible", () => {
    const turns = [
      { top: 120, bottom: 1400, live: false },
      { top: 860, bottom: 1300, live: true },
    ];
    expect(pickAnchor(turns, TOP, BOTTOM)).toBe(1);
  });

  test("a live answer scrolled out of view does not", () => {
    const turns = [
      { top: -900, bottom: 50, live: true },
      { top: 300, bottom: 600, live: false },
    ];
    expect(pickAnchor(turns, TOP, BOTTOM)).toBe(1);
  });

  test("prefers the first turn starting inside the view", () => {
    const turns = [
      { top: -200, bottom: 400, live: false },
      { top: 420, bottom: 700, live: false },
    ];
    expect(pickAnchor(turns, TOP, BOTTOM)).toBe(1);
  });

  test("falls back to a turn reaching in from above", () => {
    const turns = [{ top: -2000, bottom: 2000, live: false }];
    expect(pickAnchor(turns, TOP, BOTTOM)).toBe(0);
  });

  test("nothing on screen picks nothing", () => {
    expect(pickAnchor([], TOP, BOTTOM)).toBe(-1);
    expect(
      pickAnchor([{ top: 950, bottom: 1000, live: false }], TOP, BOTTOM),
    ).toBe(-1);
  });
});

describe("roomFor", () => {
  test("lets a turn scroll to the top of the view", () => {
    // A turn 3000px into the content, an 800px view with 80px over the top and 28px
    // under the bottom: the content must reach 3000 + 692 for it to sit at the top.
    expect(roomFor(3000, 800, 80, 28)).toBe(3692);
  });

  test("never goes negative", () => {
    expect(roomFor(0, 50, 80, 28)).toBe(0);
  });
});

/**
 * Letting go of the follow.
 *
 * A rule that yielded on distance alone once made a small scroll-up impossible: the next
 * growth put the view straight back at the bottom. Any upward movement the operator makes
 * lets go; only a thread switch or the jump control takes the follow back (`nextFollow`).
 */
describe("the follow lets go of any upward movement the operator makes", () => {
  /** Pinned at the bottom of a 5000px transcript in an 800px viewport. */
  const atBottom = {
    pinned: true,
    operator: true,
    lastTop: 4200,
    lastHeight: 5000,
    top: 4200,
    height: 5000,
  };

  test("a 40px nudge up detaches", () => {
    expect(nextPinned({ ...atBottom, top: 4160 })).toBe(false);
  });

  test("even a 3px nudge detaches — there is no gesture to out-scroll", () => {
    expect(nextPinned({ ...atBottom, top: 4197 })).toBe(false);
  });

  test("sub-pixel drift does not", () => {
    expect(nextPinned({ ...atBottom, top: 4199 })).toBe(true);
  });

  test("the follow's own write is not read back as movement", () => {
    const grown = { ...atBottom, lastTop: 4700, top: 4700, height: 5500 };
    expect(nextPinned(grown)).toBe(true);
  });

  test("the browser moving the view up on its own does not detach", () => {
    const clamped = { ...atBottom, operator: false, top: 4195, height: 5136 };
    expect(nextPinned(clamped)).toBe(true);
  });

  test("a fold closing clamps the view up without detaching", () => {
    expect(
      nextPinned({ ...atBottom, top: 3400, height: 4200, lastHeight: 5000 }),
    ).toBe(true);
  });

  test("scrolling back to the bottom does not re-attach", () => {
    const returning = {
      ...atBottom,
      pinned: false,
      lastTop: 1000,
      top: 4200,
    };
    expect(nextPinned(returning)).toBe(false);
  });
});

describe("nextFollow", () => {
  const opened = { pinned: true, chosen: false };
  const following = { pinned: true, chosen: true };

  test("a send lets go of the follow that came with opening the thread", () => {
    expect(nextFollow(opened, "send")).toEqual({
      pinned: false,
      chosen: false,
    });
  });

  test("a send keeps a follow the operator chose", () => {
    expect(nextFollow(following, "send")).toEqual(following);
  });

  test("the jump control chooses the follow", () => {
    expect(nextFollow({ pinned: false, chosen: false }, "follow")).toEqual(
      following,
    );
  });

  test("a jump to the end pins without choosing", () => {
    expect(nextFollow({ pinned: false, chosen: false }, "jump")).toEqual(
      opened,
    );
    expect(nextFollow(following, "jump")).toEqual(following);
  });

  test("letting go forgets the choice", () => {
    expect(nextFollow(following, "letGo")).toEqual({
      pinned: false,
      chosen: false,
    });
  });

  test("opening a thread pins without a choice", () => {
    expect(nextFollow(following, "open")).toEqual(opened);
  });
});

describe("firstReaching", () => {
  const bottoms = [100, 300, 500, 900];
  const at = (i: number) => bottoms[i];

  test("finds the first box reaching below the top", () => {
    expect(firstReaching(4, at, 300)).toBe(2);
    expect(firstReaching(4, at, 299)).toBe(1);
  });

  test("answers the count when every box is above", () => {
    expect(firstReaching(4, at, 900)).toBe(4);
    expect(firstReaching(0, at, 0)).toBe(0);
  });
});
