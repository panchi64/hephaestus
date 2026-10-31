import { describe, expect, test } from "bun:test";
import { isPrivateLocked } from "./api";
import {
  applyStatus,
  lockedByOperator,
  markLocked,
  pinRequest,
  privateUnlocked,
  requestPin,
  settlePin,
} from "./store";

const OPEN = {
  pin_set: true,
  unlocked: true,
  relock_at: "2026-10-31T12:15:00Z",
  relock_minutes: 15,
};

describe("the PIN prompt", () => {
  test("resolves with whether the area opened", async () => {
    const asked = requestPin("unlock");
    expect(pinRequest()?.mode).toBe("unlock");
    settlePin(true);
    expect(await asked).toBe(true);
    expect(pinRequest()).toBeNull();
  });

  test("a second request dismisses the first", async () => {
    const first = requestPin("unlock");
    const second = requestPin("set");
    expect(await first).toBe(false);
    expect(pinRequest()?.mode).toBe("set");
    settlePin(false);
    expect(await second).toBe(false);
  });
});

describe("the lock", () => {
  test("markLocked shuts an open area without forgetting the PIN", () => {
    applyStatus(OPEN);
    expect(privateUnlocked()).toBe(true);
    markLocked();
    expect(privateUnlocked()).toBe(false);
  });

  test("an operator's lock is marked until the area opens again", () => {
    const shut = { ...OPEN, unlocked: false, relock_at: null };
    applyStatus(OPEN);
    applyStatus(shut, { byOperator: true });
    expect(lockedByOperator()).toBe(true);
    applyStatus(shut);
    expect(lockedByOperator()).toBe(true);
    applyStatus(OPEN);
    expect(lockedByOperator()).toBe(false);
    markLocked();
    expect(lockedByOperator()).toBe(false);
  });

  test("only the private_locked refusal reads as the area being locked", () => {
    expect(isPrivateLocked({ status: 403, detail: "private_locked" })).toBe(
      true,
    );
    expect(isPrivateLocked({ status: 403, detail: "wrong PIN" })).toBe(false);
    expect(isPrivateLocked(new Error("private_locked"))).toBe(false);
  });
});
