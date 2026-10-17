import { describe, expect, test } from "bun:test";
import { nextPinned } from "./transcriptScroll";

/**
 * Letting go of the stream.
 *
 * The rule this replaced yielded on distance alone, and the bug it caused is the one
 * case worth naming in a test: a scroll-up smaller than the re-attach threshold left the
 * follow attached, so the next fragment put the view straight back at the bottom and the
 * transcript could not be left while a turn ran.
 */
describe("the follow lets go of any upward movement it did not cause", () => {
  /** Pinned at the bottom of a 5000px transcript in an 800px viewport. */
  const atBottom = {
    pinned: true,
    operator: true,
    lastTop: 4200,
    lastHeight: 5000,
    top: 4200,
    height: 5000,
    clientHeight: 800,
  };

  test("a 40px nudge up detaches, though it stays inside ATTACHED_PX", () => {
    expect(nextPinned({ ...atBottom, top: 4160 })).toBe(false);
  });

  test("even a 3px nudge detaches — there is no gesture to out-scroll", () => {
    expect(nextPinned({ ...atBottom, top: 4197 })).toBe(false);
  });

  test("sub-pixel drift does not", () => {
    expect(nextPinned({ ...atBottom, top: 4199 })).toBe(true);
  });

  test("the follow's own write is not read back as movement", () => {
    // `followBottom` records where it put the container, so the scroll event it
    // provokes arrives with `top === lastTop`.
    const grown = { ...atBottom, lastTop: 4700, top: 4700, height: 5500 };
    expect(nextPinned(grown)).toBe(true);
  });

  test("the browser clamping the view up while the turn grows does not detach", () => {
    // A stopped turn reloads, lays out shorter for a moment, and grows taller again:
    // the scroll event sees the view higher and the content taller, and nobody scrolled.
    const clamped = { ...atBottom, operator: false, top: 4195, height: 5136 };
    expect(nextPinned(clamped)).toBe(true);
  });

  test("content growing under a detached transcript leaves it detached", () => {
    const detached = {
      ...atBottom,
      pinned: false,
      lastTop: 1000,
      top: 1000,
      height: 5500,
    };
    expect(nextPinned(detached)).toBe(false);
  });

  test("a fold closing clamps the view up without detaching", () => {
    // The one upward movement the operator did not ask for: `scrollHeight` shrinks
    // below `scrollTop` and the browser pulls the view back on its own.
    expect(
      nextPinned({ ...atBottom, top: 3400, height: 4200, lastHeight: 5000 }),
    ).toBe(true);
  });

  test("scrolling back to the bottom re-attaches", () => {
    const returning = {
      ...atBottom,
      pinned: false,
      lastTop: 1000,
      top: 4150,
      height: 5000,
    };
    expect(nextPinned(returning)).toBe(true);
  });

  test("scrolling down but not all the way back stays detached", () => {
    const returning = {
      ...atBottom,
      pinned: false,
      lastTop: 1000,
      top: 3000,
      height: 5000,
    };
    expect(nextPinned(returning)).toBe(false);
  });
});
