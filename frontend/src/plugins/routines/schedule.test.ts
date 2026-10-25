import { describe, expect, it } from "bun:test";
import {
  agentFromValue,
  agentLabel,
  agentOptions,
  browserTimezone,
  humanizeSchedule,
  isDraft,
  secondsToValueUnit,
} from "./schedule";

describe("humanizeSchedule", () => {
  it("names the zone a cron is read in", () => {
    expect(
      humanizeSchedule({
        type: "cron",
        cron: "0 8 * * *",
        timezone: "Europe/Paris",
      }),
    ).toBe("0 8 * * * (Europe/Paris)");
  });

  it("falls back to the bare cron without a zone", () => {
    expect(
      humanizeSchedule({ type: "cron", cron: "0 8 * * *", timezone: "" }),
    ).toBe("0 8 * * *");
  });

  it("summarizes intervals and webhooks", () => {
    expect(
      humanizeSchedule({
        type: "interval",
        everySeconds: 7200,
        timezone: "UTC",
      }),
    ).toBe("every 2 hours");
    expect(humanizeSchedule({ type: "webhook", timezone: "UTC" })).toBe(
      "webhook",
    );
  });
});

describe("secondsToValueUnit", () => {
  it("picks the largest whole unit", () => {
    expect(secondsToValueUnit(86400)).toEqual({ value: "1", unit: "days" });
    expect(secondsToValueUnit(90)).toEqual({ value: "90", unit: "seconds" });
  });
});

describe("agent helpers", () => {
  it("labels null as the default agent", () => {
    expect(agentLabel(null)).toBe("Default agent");
    expect(agentLabel("reviewer")).toBe("reviewer");
  });

  it("lists the default agent first", () => {
    expect(agentOptions(["a", "b"], null)).toEqual([
      { value: "", label: "Default agent" },
      { value: "a", label: "a" },
      { value: "b", label: "b" },
    ]);
  });

  it("keeps a current agent that is no longer listed", () => {
    expect(agentOptions(["a"], "gone").map((o) => o.value)).toEqual([
      "",
      "a",
      "gone",
    ]);
  });

  it("maps the picker value back to null for the default", () => {
    expect(agentFromValue("")).toBeNull();
    expect(agentFromValue("a")).toBe("a");
  });
});

describe("isDraft", () => {
  it("is true until approved", () => {
    expect(isDraft({ approved: false })).toBe(true);
    expect(isDraft({ approved: true })).toBe(false);
  });
});

describe("browserTimezone", () => {
  it("returns an IANA zone name", () => {
    expect(browserTimezone().length).toBeGreaterThan(0);
  });
});
