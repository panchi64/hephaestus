/** The requests behind the Private area, each settling the store from the backend's
 *  answer rather than guessing at it. */

import { isPrivateLocked, privateApi } from "./api";
import { applyStatus, pinSet, privateUnlocked, requestPin } from "./store";

export async function refreshPrivateStatus(): Promise<void> {
  applyStatus(await privateApi.status());
}

export async function unlockPrivate(pin: string): Promise<void> {
  applyStatus(await privateApi.unlock(pin));
}

export async function lockPrivate(): Promise<void> {
  applyStatus(await privateApi.lock(), { byOperator: true });
}

export async function setPrivatePin(
  pin: string,
  proof: { currentPin?: string; password?: string } = {},
): Promise<void> {
  applyStatus(await privateApi.setPin(pin, proof));
}

export async function setRelockMinutes(minutes: number): Promise<void> {
  applyStatus(await privateApi.setRelock(minutes));
}

/** Open the area if it isn't, asking for the PIN, or for a first one when none is set.
 *  Resolves whether it is open. */
export async function ensureUnlocked(): Promise<boolean> {
  await refreshPrivateStatus();
  if (privateUnlocked()) return true;
  return requestPin(pinSet() ? "unlock" : "set");
}

/** Run a request that may name a private thread; if the area turns out to be locked,
 *  ask for the PIN and run it once more. A dismissed prompt rethrows the refusal. */
export async function withPrivateAccess<T>(run: () => Promise<T>): Promise<T> {
  try {
    return await run();
  } catch (err) {
    if (!isPrivateLocked(err)) throw err;
    await refreshPrivateStatus();
    if (!(await requestPin("unlock"))) throw err;
    return run();
  }
}
