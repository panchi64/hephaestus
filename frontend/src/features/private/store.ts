/**
 * The Private area as the client knows it: the backend's last word on its lock, the PIN
 * prompt that is open, and whether the next new thread is filed in the area.
 *
 * State transitions only. The requests live in `actions.ts` and the relock timer in
 * `useRelockTimer.ts`.
 */

import { createSignal } from "solid-js";
import type { PrivateStatusDTO } from "./api";

/** `unlock` opens the area, `set` chooses the first PIN, `change` replaces it. */
export type PinMode = "unlock" | "set" | "change";

interface PinRequest {
  mode: PinMode;
  resolve: (opened: boolean) => void;
}

const [status, setStatus] = createSignal<PrivateStatusDTO | undefined>();
const [pinRequest, setPinRequest] = createSignal<PinRequest | null>(null);
const [stagedPrivate, setStagedPrivate] = createSignal(false);
const [lockedByOperator, setLockedByOperator] = createSignal(false);

export { lockedByOperator, pinRequest, stagedPrivate, setStagedPrivate };
export const privateStatus = status;
export const privateUnlocked = (): boolean => status()?.unlocked ?? false;
export const pinSet = (): boolean => status()?.pin_set ?? false;

/** Take the backend's answer. `byOperator` marks a lock the operator asked for, which
 *  nothing should offer to undo; the mark lasts until the area opens again. Set before
 *  the status, so whatever reacts to the lock reads the mark already in place. */
export function applyStatus(
  next: PrivateStatusDTO,
  { byOperator = false }: { byOperator?: boolean } = {},
): void {
  setLockedByOperator(!next.unlocked && (byOperator || lockedByOperator()));
  setStatus(next);
}

/** The relock time passed: the area is shut without asking the backend. */
export function markLocked(): void {
  const current = status();
  if (!current?.unlocked) return;
  setLockedByOperator(false);
  setStatus({ ...current, unlocked: false, relock_at: null });
}

/** Open the PIN prompt; resolves true once the area is open, false if dismissed. A
 *  second request replaces the first, which resolves false. */
export function requestPin(mode: PinMode): Promise<boolean> {
  pinRequest()?.resolve(false);
  return new Promise((resolve) => setPinRequest({ mode, resolve }));
}

export function settlePin(opened: boolean): void {
  const request = pinRequest();
  setPinRequest(null);
  request?.resolve(opened);
}
