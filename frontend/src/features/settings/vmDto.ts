import type { Status } from "~/ui";
import type { VmState, VmStatus } from "./model";

/** `GET /vm` and `POST /vm/reset` share this body. */
export interface VmStateDTO {
  state: string;
  detail: string | null;
}

/** The single wire→model mapper. A state this client does not know reads as
 *  unavailable rather than as healthy. */
export function toVmState(dto: VmStateDTO): VmState {
  const state: VmStatus =
    dto.state === "ready" || dto.state === "starting"
      ? dto.state
      : "unavailable";
  return { state, detail: dto.detail?.trim() || null };
}

export function vmStateLabel(state: VmStatus): string {
  switch (state) {
    case "ready":
      return "Ready";
    case "starting":
      return "Starting…";
    case "unavailable":
      return "Unavailable";
  }
}

export function vmStateTone(state: VmStatus): Status {
  switch (state) {
    case "ready":
      return "nominal";
    case "starting":
      return "info";
    case "unavailable":
      return "alert";
  }
}

/** Only a VM that is coming up changes by itself, so only that is polled. */
export function vmNeedsPolling(state: VmStatus | undefined): boolean {
  return state === "starting";
}
