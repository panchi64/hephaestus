import { createResource, createSignal, type Resource } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { ALL_PROJECTS, getProjectScope } from "~/lib/api/projectScope";
import type {
  Agent,
  AgentSource,
  AgentWorkspace,
  AgentWrite,
  PermissionCeiling,
} from "./model";

/* ── Backend DTO → seam type ──────────────────────────────────────────────── */

export interface AgentOut {
  id: string;
  name: string;
  description: string;
  instructions: string;
  endpoint_id: string | null;
  model: string | null;
  tools: string[] | null;
  deny_tools: string[];
  permission_ceiling: PermissionCeiling | null;
  workspace: AgentWorkspace;
  request_limit: number | null;
  wall_clock_timeout_s: number | null;
  can_delegate: boolean;
  max_depth: number | null;
  delegable: boolean;
  source: AgentSource;
  published: boolean;
  active: boolean;
  editable: boolean;
}

export function toAgent(dto: AgentOut): Agent {
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description,
    instructions: dto.instructions,
    endpointId: dto.endpoint_id,
    model: dto.model,
    tools: dto.tools,
    denyTools: dto.deny_tools,
    permissionCeiling: dto.permission_ceiling,
    workspace: dto.workspace,
    requestLimit: dto.request_limit,
    wallClockTimeoutS: dto.wall_clock_timeout_s,
    canDelegate: dto.can_delegate,
    maxDepth: dto.max_depth,
    delegable: dto.delegable,
    source: dto.source,
    published: dto.published,
    active: dto.active,
    editable: dto.editable,
  };
}

/** The project whose file-declared agents the listing should include: the
 *  operator's active scope, unless it is deliberately unscoped. */
export function activeProjectId(): string | undefined {
  const scope = getProjectScope();
  return scope && scope !== ALL_PROJECTS ? scope : undefined;
}

export function agentsPath(projectId: string | undefined): string {
  return projectId
    ? `/agents?project_id=${encodeURIComponent(projectId)}`
    : "/agents";
}

/* ── List ─────────────────────────────────────────────────────────────────── */

const [listTick, setListTick] = createSignal(0);

async function fetchAgents(projectId: string | undefined): Promise<Agent[]> {
  const rows = await api.get<AgentOut[]>(agentsPath(projectId));
  return rows.map(toAgent);
}

export function useAgents(
  projectId: () => string | undefined = activeProjectId,
): Resource<Agent[]> {
  const [data] = createResource(
    () => [projectId() ?? "", listTick()] as const,
    ([project]) => fetchAgents(project || undefined),
  );
  return data;
}

export function refreshAgents(): void {
  setListTick((n) => n + 1);
}

/* ── Detail ───────────────────────────────────────────────────────────────── */

const [detailTick, setDetailTick] = createSignal(0);

async function fetchAgent(id: string): Promise<Agent | undefined> {
  try {
    return toAgent(
      await api.get<AgentOut>(`/agents/${encodeURIComponent(id)}`),
    );
  } catch (err) {
    if (isApiError(err) && err.status === 404) return undefined;
    throw err;
  }
}

/** Single agent for the editor. Resolves `undefined` for an unknown id. */
export function useAgentDetail(id: () => string): Resource<Agent | undefined> {
  const [data] = createResource(
    () => [id(), detailTick()] as const,
    ([agentId]) => fetchAgent(agentId),
  );
  return data;
}

function refreshAll(): void {
  setListTick((n) => n + 1);
  setDetailTick((n) => n + 1);
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

/** Always lands as a draft. */
export async function createAgent(body: AgentWrite): Promise<Agent> {
  const dto = await api.post<AgentOut>("/agents", body);
  refreshAll();
  return toAgent(dto);
}

export async function updateAgent(
  id: string,
  patch: AgentWrite,
): Promise<Agent> {
  const dto = await api.patch<AgentOut>(
    `/agents/${encodeURIComponent(id)}`,
    patch,
  );
  refreshAll();
  return toAgent(dto);
}

export async function setAgentPublished(
  id: string,
  published: boolean,
): Promise<Agent> {
  const dto = await api.post<AgentOut>(
    `/agents/${encodeURIComponent(id)}/${published ? "publish" : "unpublish"}`,
  );
  refreshAll();
  return toAgent(dto);
}

export async function deleteAgent(id: string): Promise<void> {
  await api.del(`/agents/${encodeURIComponent(id)}`);
  refreshAll();
}

/* ── Errors ───────────────────────────────────────────────────────────────── */

/** The backend's message for a failure, verbatim; `fallback` covers a
 *  transport failure that produced none. */
export function agentErrorMessage(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}

/** The input a 422 blamed, when it named one. */
export function agentErrorField(err: unknown): string | null {
  return isApiError(err) ? (err.field ?? null) : null;
}
