import { describe, expect, test } from "bun:test";
import { webSearchReadout } from "./model";

describe("webSearchReadout", () => {
  test("reads as built in when no instance of the operator's is enabled", () => {
    expect(
      webSearchReadout({ status: "nominal", detail: "SearXNG (managed)" }, 0),
    ).toEqual({ source: "built-in", label: "Built in", tone: "nominal" });
  });

  test("an enabled operator instance overrides the built-in one", () => {
    expect(
      webSearchReadout({ status: "nominal", detail: "SearXNG configured" }, 1),
    ).toEqual({ source: "own", label: "Your instance", tone: "nominal" });
  });

  test("a degraded capability shows the backend's reason", () => {
    expect(webSearchReadout({ status: "warn", detail: "starting" }, 0)).toEqual(
      { source: "built-in", label: "starting", tone: "warn" },
    );
  });

  test("an unread capability is still being checked", () => {
    expect(webSearchReadout(null, 0).label).toBe("Checking");
  });
});
