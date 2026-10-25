import { createSignal } from "solid-js";
import { api, isApiError } from "~/lib/api";
import type { LocalServer } from "./model";

interface OnboardingStateDTO {
  completed: boolean;
}

interface LocalServerDTO {
  label: string;
  base_url: string;
  models: {
    id: string;
    context_window: number | null;
    parallel_slots: number | null;
    loaded: boolean | null;
    kind: string | null;
  }[];
}

/** `null` until the gate has asked; a plain signal so reopening from Settings swaps the
 *  app for the walkthrough in the same tick. */
const [completed, setCompleted] = createSignal<boolean | null>(null);

export const onboardingCompleted = completed;

/** A backend that can't answer lets the operator in: the walkthrough is a convenience,
 *  and the send gate still explains a missing model. */
export async function loadOnboarding(): Promise<void> {
  // A reset and a fresh setup in the same tab must not reuse the last workspace's answer.
  setCompleted(null);
  try {
    setCompleted((await api.get<OnboardingStateDTO>("/onboarding")).completed);
  } catch (err) {
    if (isApiError(err) && (err.status === 401 || err.status === 423)) return;
    setCompleted(true);
  }
}

export async function completeOnboarding(): Promise<void> {
  await api.post<OnboardingStateDTO>("/onboarding/complete");
  setCompleted(true);
}

export async function reopenOnboarding(): Promise<void> {
  await api.post<OnboardingStateDTO>("/onboarding/reopen");
  setCompleted(false);
}

export async function fetchLocalServers(): Promise<LocalServer[]> {
  const rows = await api.get<LocalServerDTO[]>("/onboarding/local-endpoints");
  return rows.map((r) => ({
    label: r.label,
    baseUrl: r.base_url,
    models: r.models.map((m) => ({
      id: m.id,
      contextWindow: m.context_window,
      parallelSlots: m.parallel_slots,
      loaded: m.loaded,
      kind: m.kind,
    })),
  }));
}
