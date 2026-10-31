import { api, isApiError } from "~/lib/api";

export interface PrivateStatusDTO {
  pin_set: boolean;
  unlocked: boolean;
  relock_at: string | null;
  relock_minutes: number;
}

/** The detail the backend refuses a private thread with while the area is locked. */
export const PRIVATE_LOCKED = "private_locked";

export function isPrivateLocked(err: unknown): boolean {
  return isApiError(err) && err.status === 403 && err.detail === PRIVATE_LOCKED;
}

export const privateApi = {
  status: () => api.get<PrivateStatusDTO>("/private/status"),
  unlock: (pin: string) =>
    api.post<PrivateStatusDTO>("/private/unlock", { pin }),
  lock: () => api.post<PrivateStatusDTO>("/private/lock"),
  setPin: (pin: string, proof: { currentPin?: string; password?: string }) =>
    api.post<PrivateStatusDTO>("/private/pin", {
      pin,
      current_pin: proof.currentPin,
      password: proof.password,
    }),
  setRelock: (minutes: number) =>
    api.put<PrivateStatusDTO>("/private/relock", { minutes }),
};
