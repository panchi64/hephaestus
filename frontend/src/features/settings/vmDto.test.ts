import { describe, expect, test } from "bun:test";
import { toVmState, vmNeedsPolling, vmStateLabel, vmStateTone } from "./vmDto";

describe("reading the platform VM state", () => {
  test("each known state passes through with its detail", () => {
    expect(toVmState({ state: "ready", detail: null })).toEqual({
      state: "ready",
      detail: null,
    });
    expect(toVmState({ state: "starting", detail: "Booting" })).toEqual({
      state: "starting",
      detail: "Booting",
    });
    expect(
      toVmState({ state: "unavailable", detail: "No hypervisor" }),
    ).toEqual({ state: "unavailable", detail: "No hypervisor" });
  });

  test("a state the client does not know reads as unavailable", () => {
    expect(toVmState({ state: "paused", detail: null }).state).toBe(
      "unavailable",
    );
  });

  test("blank detail text collapses to null", () => {
    expect(toVmState({ state: "ready", detail: "   " }).detail).toBeNull();
  });
});

describe("presenting the state", () => {
  test("labels and tones", () => {
    expect(vmStateLabel("ready")).toBe("Ready");
    expect(vmStateLabel("starting")).toBe("Starting…");
    expect(vmStateLabel("unavailable")).toBe("Unavailable");
    expect(vmStateTone("ready")).toBe("nominal");
    expect(vmStateTone("starting")).toBe("info");
    expect(vmStateTone("unavailable")).toBe("alert");
  });

  test("only a starting VM is polled", () => {
    expect(vmNeedsPolling("starting")).toBe(true);
    expect(vmNeedsPolling("ready")).toBe(false);
    expect(vmNeedsPolling("unavailable")).toBe(false);
    expect(vmNeedsPolling(undefined)).toBe(false);
  });
});
