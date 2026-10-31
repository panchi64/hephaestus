import { describe, expect, it } from "bun:test";
import { effortOptions } from "~/lib/effort";
import { effortToSend, seatEffort, type EffortSeat } from "./effortSeat";

describe("seatEffort", () => {
  it("seats an opened thread from its stored level, which then owns the control", () => {
    expect(seatEffort({ currentId: "b", owner: "a", stored: "max" })).toEqual({
      value: "max",
      owner: "b",
    });
  });

  it("seats Default while the thread loads, still waiting for its level", () => {
    expect(
      seatEffort({ currentId: "b", owner: "a", stored: undefined }),
    ).toEqual({ value: "", owner: "a" });
    expect(seatEffort({ currentId: "b", owner: "a", stored: "" })).toEqual({
      value: "",
      owner: "b",
    });
  });

  it("leaves the operator's pick for the thread on screen alone", () => {
    expect(
      seatEffort({ currentId: "a", owner: "a", stored: "low" }),
    ).toBeNull();
  });

  it("re-seats a thread the operator comes back to with its own level", () => {
    // Picked max on A, then opened B (stored low), then came back to A (stored max).
    let owner: string | null = "a";
    let value = "max";
    const visit = (currentId: string, stored: string | undefined): void => {
      const seat: EffortSeat | null = seatEffort({ currentId, owner, stored });
      if (seat === null) return;
      owner = seat.owner;
      value = seat.value;
    };
    visit("b", undefined);
    visit("b", "low");
    visit("a", undefined);
    expect(
      effortToSend({ choice: value, owner, currentId: "a" }),
    ).toBeUndefined();
    visit("a", "max");
    expect(value).toBe("max");
    expect(effortToSend({ choice: value, owner, currentId: "a" })).toBe("max");
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
