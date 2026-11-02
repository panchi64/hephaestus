import { describe, expect, test } from "bun:test";
import type { ServiceHealth } from "../model";
import { overallHealth } from "./status";

const row = (over: Partial<ServiceHealth>): ServiceHealth => ({
  key: "chat_model",
  kind: "capability",
  label: "CHAT MODEL",
  status: "nominal",
  detail: "",
  critical: true,
  ...over,
});

describe("the Health page's overall status", () => {
  test("a down capability is an alert", () => {
    expect(overallHealth([row({ status: "alert" })])).toBe("alert");
  });

  test("a failed endpoint test is a warning, not a workspace alert", () => {
    const failed = row({
      key: "endpoint:spare",
      kind: "endpoint",
      status: "alert",
      critical: false,
    });
    expect(overallHealth([row({}), failed])).toBe("warn");
  });

  test("all nominal is nominal", () => {
    expect(
      overallHealth([row({}), row({ key: "endpoint:a", kind: "endpoint" })]),
    ).toBe("nominal");
  });
});
