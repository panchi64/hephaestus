// Pin the suite's timezone. `bun test` runs at UTC by default, which is the one zone
// where every local/UTC confusion in this module looks correct — `parseInstant`'s
// regression test cannot fail there, and the local-time fixtures below pass for the
// wrong reason. Pinning to a fixed offset zone makes both real, and makes the whole
// file deterministic rather than dependent on whichever zone the runner picked.
//
// It does NOT matter that `import` declarations are hoisted above this line and so run
// first: the engine resolves the zone lazily, per `Date` operation, not once at module
// evaluation. What would break the pin is a `Date` value *computed* at import time and
// reused — there is none here, and the fixtures below build their dates inside `it`.
process.env.TZ = "America/New_York";

import { describe, expect, it } from "bun:test";
import {
  longTimestamp,
  met,
  parseInstant,
  plural,
  relativeTime,
} from "./index";

/** Built from local-time parts on purpose. `longTimestamp` reads the operator's clock
 *  (see its docstring), so a fixture written as a UTC string would shift the expected
 *  output — and the assertions below — with the machine running the suite. */
function local(
  y: number,
  monthIndex: number,
  day: number,
  h = 12,
  min = 0,
): string {
  return new Date(y, monthIndex, day, h, min).toISOString();
}

describe("longTimestamp", () => {
  it("writes the stamp out in full", () => {
    expect(longTimestamp(local(2026, 8, 10, 21, 3))).toBe(
      "Thu, Sept 10th 2026 @ 9:03PM",
    );
  });

  it("spells the months the way the product writes them", () => {
    // `Intl`'s `short` gives `Sep`/`Jun`/`Jul`; these three are the ones that differ.
    expect(longTimestamp(local(2026, 5, 1))).toContain("June");
    expect(longTimestamp(local(2026, 6, 1))).toContain("July");
    expect(longTimestamp(local(2026, 8, 1))).toContain("Sept");
    expect(longTimestamp(local(2026, 0, 1))).toContain("Jan");
  });

  it("takes th for the teens and the matching suffix elsewhere", () => {
    const day = (n: number) => longTimestamp(local(2026, 0, n)).split(" ")[2];
    expect(day(1)).toBe("1st");
    expect(day(2)).toBe("2nd");
    expect(day(3)).toBe("3rd");
    expect(day(4)).toBe("4th");
    // The teens are the case the naive mod-10 rule gets wrong.
    expect(day(11)).toBe("11th");
    expect(day(12)).toBe("12th");
    expect(day(13)).toBe("13th");
    expect(day(21)).toBe("21st");
    expect(day(22)).toBe("22nd");
    expect(day(23)).toBe("23rd");
    expect(day(31)).toBe("31st");
  });

  it("puts midnight and noon on 12, not 0", () => {
    expect(longTimestamp(local(2026, 0, 1, 0, 5))).toContain("12:05AM");
    expect(longTimestamp(local(2026, 0, 1, 12, 5))).toContain("12:05PM");
    expect(longTimestamp(local(2026, 0, 1, 23, 59))).toContain("11:59PM");
    expect(longTimestamp(local(2026, 0, 1, 1, 0))).toContain("1:00AM");
  });

  it("zero-pads the minute so the column stays aligned", () => {
    expect(longTimestamp(local(2026, 0, 1, 9, 3))).toContain("9:03AM");
  });

  it("hands back unparseable input rather than NaN", () => {
    // Matching `timestamp`/`date`/`relativeTime` beside it: a bad stamp shows the raw
    // value, which is at least diagnosable, instead of "Invalid Date".
    expect(longTimestamp("not a date")).toBe("not a date");
    expect(longTimestamp("")).toBe("");
  });
});

describe("met", () => {
  const SEC = 1000;
  const MIN = 60 * SEC;
  const HOUR = 60 * MIN;

  it("always renders all three fields, zero-padded", () => {
    expect(met(0)).toBe("00:00:00");
    expect(met(9 * SEC)).toBe("00:00:09");
    expect(met(9 * MIN + 9 * SEC)).toBe("00:09:09");
    expect(met(4 * HOUR + 21 * MIN + 7 * SEC)).toBe("04:21:07");
  });

  it("keeps a constant width as it ticks, which is the point", () => {
    // The clock is read by position. Every value under 100 hours must measure the
    // same, or the figures beside it jump once a second.
    const widths = new Set(
      [0, SEC, MIN, HOUR, 59 * MIN + 59 * SEC, 99 * HOUR].map(
        (ms) => met(ms).length,
      ),
    );
    expect(widths).toEqual(new Set([8]));
  });

  it("does not wrap hours into days", () => {
    // A session running past 24h says so, rather than hiding behind a day counter
    // the operator has to add back.
    expect(met(27 * HOUR + 14 * MIN + 2 * SEC)).toBe("27:14:02");
    expect(met(132 * HOUR + 9 * SEC)).toBe("132:00:09");
  });

  it("truncates rather than rounds, so a field never reads ahead of itself", () => {
    expect(met(1999)).toBe("00:00:01");
    expect(met(59 * MIN + 59_999)).toBe("00:59:59");
  });

  it("clamps a negative elapsed to zero", () => {
    // Clock skew between host and backend is not something to render as time travel.
    expect(met(-1)).toBe("00:00:00");
    expect(met(-5 * HOUR)).toBe("00:00:00");
  });
});

describe("parseInstant", () => {
  it("reads a zone-less backend timestamp as UTC, not local", () => {
    // The regression this exists for: SQLite strips the tzinfo, a route that misses
    // `as_utc` ships `2026-09-19T13:57:41.480578`, and `Date.parse` calls that local.
    // West of Greenwich the bare parse lands in the future and every elapsed clock
    // pins to zero.
    //
    // Asserted against `Date.UTC`, which takes no timezone from the host. Comparing
    // against `Date.parse("…Z")` would have been the natural spelling and is the wrong
    // one: on a machine running UTC the buggy and correct answers are identical, so the
    // fixture could only fail where the bug happened to reproduce.
    expect(parseInstant("2026-09-19T13:57:41.480578")).toBe(
      Date.UTC(2026, 8, 19, 13, 57, 41, 480),
    );
    expect(parseInstant("2026-09-19T13:57:41")).toBe(
      Date.UTC(2026, 8, 19, 13, 57, 41),
    );
  });

  it("leaves a timestamp that already carries a zone alone", () => {
    const utc = Date.parse("2026-09-19T13:57:41Z");
    expect(parseInstant("2026-09-19T13:57:41Z")).toBe(utc);
    expect(parseInstant("2026-09-19T13:57:41z")).toBe(utc);
    expect(parseInstant("2026-09-19T09:57:41-04:00")).toBe(utc);
    expect(parseInstant("2026-09-19T09:57:41-0400")).toBe(utc);
    expect(parseInstant("2026-09-19T15:57:41+02:00")).toBe(utc);
  });

  it("leaves a date-only string alone — the spec already calls that UTC", () => {
    expect(parseInstant("2026-09-19")).toBe(Date.parse("2026-09-19"));
  });

  it("hands back NaN for junk rather than a wrong instant", () => {
    expect(Number.isNaN(parseInstant("not a date"))).toBe(true);
    expect(Number.isNaN(parseInstant(""))).toBe(true);
  });
});

describe("plural", () => {
  it("agrees the noun with the count, zero included", () => {
    expect(plural(1, "source")).toBe("1 source");
    expect(plural(0, "topic")).toBe("0 topics");
    expect(plural(4, "origin")).toBe("4 origins");
  });

  it("takes an irregular plural when given one", () => {
    expect(plural(2, "match", "matches")).toBe("2 matches");
    expect(plural(1, "match", "matches")).toBe("1 match");
  });
});

describe("relativeTime", () => {
  it("reads a past instant as how long ago", () => {
    expect(
      relativeTime("2026-06-10T07:00:00Z", new Date("2026-06-10T09:00:00Z")),
    ).toBe("2H AGO");
  });

  it("reads a future instant as how long until", () => {
    expect(
      relativeTime("2026-06-10T09:30:00Z", new Date("2026-06-10T09:00:00Z")),
    ).toBe("IN 30M");
    expect(
      relativeTime("2026-06-13T09:00:00Z", new Date("2026-06-10T09:00:00Z")),
    ).toBe("IN 3D");
  });
});
