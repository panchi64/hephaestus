import { describe, expect, it } from "bun:test";
import { GREETINGS, dayPartOf, pickGreeting } from "./greetings";

const at = (hour: number) => new Date(2026, 9, 18, hour, 30);

describe("dayPartOf", () => {
  it("splits the day at 5, 12, 17 and 22", () => {
    expect(dayPartOf(at(4))).toBe("late");
    expect(dayPartOf(at(5))).toBe("morning");
    expect(dayPartOf(at(11))).toBe("morning");
    expect(dayPartOf(at(12))).toBe("afternoon");
    expect(dayPartOf(at(16))).toBe("afternoon");
    expect(dayPartOf(at(17))).toBe("evening");
    expect(dayPartOf(at(21))).toBe("evening");
    expect(dayPartOf(at(22))).toBe("late");
    expect(dayPartOf(at(0))).toBe("late");
  });
});

describe("pickGreeting", () => {
  it("draws only from the current day part and the any-hour lines", () => {
    const allowed = new Set(
      GREETINGS.filter((g) => g.when === "morning" || g.when === "any").map(
        (g) => g.text,
      ),
    );
    for (let i = 0; i < 20; i++) {
      expect(allowed.has(pickGreeting(at(9), null, () => i / 20))).toBe(true);
    }
  });

  it("never repeats the previous greeting", () => {
    const previous = pickGreeting(at(20), null, () => 0);
    for (let i = 0; i < 20; i++) {
      expect(pickGreeting(at(20), previous, () => i / 20)).not.toBe(previous);
    }
  });

  it("stays in range when random returns its upper bound", () => {
    expect(typeof pickGreeting(at(23), null, () => 0.999999)).toBe("string");
  });
});

describe("GREETINGS", () => {
  it("has no exclamation marks and no brand name", () => {
    for (const g of GREETINGS) {
      expect(g.text).not.toContain("!");
      expect(g.text.toLowerCase()).not.toContain("hephaestus");
    }
  });
});
