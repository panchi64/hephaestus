import type {
  ModelEndpoint,
  ModelGroup,
  ModelSelection,
  RoleBinding,
} from "~/lib/stores/models";

export type StepId = "endpoint" | "chat" | "roles" | "workspace" | "ready";

export interface Step {
  id: StepId;
  label: string;
  required: boolean;
}

export const STEPS: Step[] = [
  { id: "endpoint", label: "Connect a model", required: true },
  { id: "chat", label: "Pick the chat model", required: true },
  { id: "roles", label: "Helper models", required: false },
  { id: "workspace", label: "Preferences", required: false },
  { id: "ready", label: "Review and enter", required: false },
];

export interface LocalModel {
  id: string;
  contextWindow: number | null;
  parallelSlots: number | null;
  /** The server reports it loaded right now; null when it doesn't say. */
  loaded: boolean | null;
  /** `llm`, `vlm` or `embeddings` where the server names it. */
  kind: string | null;
}

export interface LocalServer {
  label: string;
  baseUrl: string;
  models: LocalModel[];
}

export function stepIndex(id: StepId): number {
  return STEPS.findIndex((s) => s.id === id);
}

/** `03 / 05`: the rail footer's position readout. */
export function stepCounter(id: StepId): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(stepIndex(id) + 1)} / ${pad(STEPS.length)}`;
}

export function endpointReady(endpoints: ModelEndpoint[]): boolean {
  return endpoints.some((e) => e.enabled && e.lastStatus === "ok");
}

/** An explicit `main` with a window: the two things `POST /onboarding/complete` checks. */
export function chatReady(main: RoleBinding | undefined): boolean {
  return !!main && !main.implicit && main.contextWindow !== null;
}

/** Where a reopened or reloaded walkthrough picks up: the first unmet requirement, else
 *  the first optional step. */
export function resumeStep(
  endpoints: ModelEndpoint[],
  main: RoleBinding | undefined,
): StepId {
  if (!endpointReady(endpoints)) return "endpoint";
  if (!chatReady(main)) return "chat";
  return "roles";
}

/** `65536` → `64K`, `131072` → `128K`, `1000000` → `977K`. */
export function formatTokens(n: number): string {
  return n >= 1024 ? `${Math.round(n / 1024)}K` : String(n);
}

/** The scan's facts about `model` on the endpoint at `baseUrl`, when the scan saw it. */
export function scannedModel(
  servers: LocalServer[],
  baseUrl: string,
  model: string,
): LocalModel | undefined {
  const norm = (u: string) => u.replace(/\/+$/, "");
  return servers
    .find((s) => norm(s.baseUrl) === norm(baseUrl))
    ?.models.find((m) => m.id === model);
}

/** The chat picks to offer: every discovered model on a working endpoint, minus the ones
 *  the scan says are embedding models. */
export function chatChoices(
  groups: ModelGroup[],
  endpoints: ModelEndpoint[],
  servers: LocalServer[],
): ModelSelection[] {
  const ok = new Map(
    endpoints.filter((e) => e.lastStatus === "ok").map((e) => [e.id, e]),
  );
  return groups.flatMap((g) => {
    const ep = ok.get(g.endpointId);
    if (!ep) return [];
    return g.choices
      .filter(
        (c) =>
          scannedModel(servers, ep.baseUrl, c.model)?.kind !== "embeddings",
      )
      .map((c) => ({ endpointId: c.endpointId, model: c.model }));
  });
}

/** The recall picks: the models the scan names as embedding models, or every model on a
 *  working endpoint when the scan can't tell (the backend refuses one that isn't). */
export function embeddingChoices(
  groups: ModelGroup[],
  endpoints: ModelEndpoint[],
  servers: LocalServer[],
): ModelSelection[] {
  const ok = new Map(
    endpoints.filter((e) => e.lastStatus === "ok").map((e) => [e.id, e]),
  );
  const all = groups.flatMap((g) =>
    ok.has(g.endpointId)
      ? g.choices.map((c) => ({ endpointId: c.endpointId, model: c.model }))
      : [],
  );
  const named = all.filter((c) => {
    const ep = ok.get(c.endpointId);
    return (
      ep && scannedModel(servers, ep.baseUrl, c.model)?.kind === "embeddings"
    );
  });
  return named.length > 0 ? named : all;
}

/** The pick to preselect: the model a scanned server has loaded, else the first. */
export function preferredChoice(
  choices: ModelSelection[],
  endpoints: ModelEndpoint[],
  servers: LocalServer[],
): ModelSelection | null {
  const baseUrl = (id: string) => endpoints.find((e) => e.id === id)?.baseUrl;
  const loaded = choices.find((c) => {
    const url = baseUrl(c.endpointId);
    return url ? scannedModel(servers, url, c.model)?.loaded === true : false;
  });
  return loaded ?? choices[0] ?? null;
}

/** A server the scan found that no endpoint points at yet. */
export function unclaimedServers(
  servers: LocalServer[],
  endpoints: ModelEndpoint[],
): LocalServer[] {
  const norm = (u: string) => u.replace(/\/+$/, "");
  const taken = new Set(endpoints.map((e) => norm(e.baseUrl)));
  return servers.filter((s) => !taken.has(norm(s.baseUrl)));
}
