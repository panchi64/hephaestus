import { describe, expect, test } from "bun:test";
import { cautionLines, offersGrant } from "./approvalCautions";

describe("an approval's cautions", () => {
  test("a guarded call offers no standing grant", () => {
    expect(offersGrant({ guard: "reads your mail" })).toBe(false);
  });

  test("an unguarded call does, whether or not it is unconfined", () => {
    expect(offersGrant({})).toBe(true);
    expect(offersGrant({ unconfined: "the fence is off" })).toBe(true);
  });

  test("the guard line states the reason", () => {
    expect(cautionLines({ guard: "reads your mail" })).toEqual([
      { label: "Always asks", reason: "reads your mail" },
    ]);
  });

  test("the unconfined line carries the backend's text, after the guard", () => {
    expect(
      cautionLines({ guard: "g", unconfined: "the host fence is not applied" }),
    ).toEqual([
      { label: "Always asks", reason: "g" },
      { label: "Unconfined", reason: "the host fence is not applied" },
    ]);
  });

  test("an ordinary approval prints nothing", () => {
    expect(cautionLines({})).toEqual([]);
  });
});
