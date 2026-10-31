import { describe, expect, it } from "bun:test";
import { effortOptions } from "~/lib/effort";
import { effortToSend, seatEffort } from "./effortSeat";

describe("seatEffort", () => {
  it("seats an opened thread from its stored level", () => {
    expect(seatEffort({ currentId: "b", owner: "a", stored: "max" })).toBe(
      "max",
    );
  });

  it("seats Default while the thread loads or stores none", () => {
    expect(seatEffort({ currentId: "b", owner: "a", stored: undefined })).toBe(
      "",
    );
    expect(seatEffort({ currentId: "b", owner: "a", stored: "" })).toBe("");
  });

  it("leaves the operator's pick for the thread on screen alone", () => {
    expect(
      seatEffort({ currentId: "a", owner: "a", stored: "low" }),
    ).toBeNull();
  });
});

describe("effortToSend", () => {
  it("names the pick only on the thread it was made on", () => {
    expect(effortToSend({ choice: "max", owner: "a", currentId: "a" })).toBe(
      "max",
    );
    expect(
      effortToSend({ choice: "max", owner: "a", currentId: "b" }),
    ).toBeUndefined();
  });

  it("names a cleared pick as empty, which resets the thread to Default", () => {
    expect(effortToSend({ choice: "", owner: "a", currentId: "a" })).toBe("");
  });
});

describe("effortOptions", () => {
  it("labels Default with the level it stands for", () => {
    const [first, ...levels] = effortOptions("xhigh");
    expect(first).toEqual({ value: "", label: "Default (extra high)" });
    expect(levels.map((level) => level.value)).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });
});
