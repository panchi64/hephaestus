import { createResource, createSignal, type Resource } from "solid-js";
import { api, downloadContent, isApiError } from "~/lib/api";
import { ALL_PROJECTS, getProjectScope } from "~/lib/api/projectScope";
import type {
  Agent,
  AgentClass,
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
  agent_class: string;
  instructions: string;
  endpoint_id: string | null;
  model: string | null;
  tools: string[] | null;
  deny_tools: string[];
  permission_ceiling: PermissionCeiling | null;
  workspace: AgentWorkspace;
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
    agentClass: dto.agent_class,
    instructions: dto.instructions,
    endpointId: dto.endpoint_id,
    model: dto.model,
    tools: dto.tools,
    denyTools: dto.deny_tools,
    permissionCeiling: dto.permission_ceiling,
    workspace: dto.workspace,
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

/* ── Classes ──────────────────────────────────────────────────────────────── */

/** The classes an agent can take. They ship with the backend, so one read serves the
 *  whole session. */
let classesRead: Promise<AgentClass[]> | undefined;

function readClasses(): Promise<AgentClass[]> {
  classesRead ??= api
    .get<AgentClass[]>("/agents/classes")
    .catch((err: unknown) => {
      classesRead = undefined;
      throw err;
    });
  return classesRead;
}

export function useAgentClasses(): Resource<AgentClass[]> {
  const [data] = createResource(readClasses);
  return data;
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

/** Refetches the open agent after a change made outside this module. */
export function refreshAgentDetail(): void {
  refreshAll();
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

/* ── Document, import, export ─────────────────────────────────────────────── */

/** The agent as its `.md` file — frontmatter and instructions. Works for any
 *  id, including declared ones such as `builtin:reviewer`. */
export async function getAgentDocument(id: string): Promise<string> {
  const doc = await api.get<{ content: string }>(
    `/agents/${encodeURIComponent(id)}/document`,
  );
  return doc.content;
}

/** Rewrite a stored agent from its edited file. A rejection is a 422 naming
 *  the field (`file` for text that is not an agent file at all). */
export async function putAgentDocument(
  id: string,
  content: string,
): Promise<Agent> {
  const dto = await api.put<AgentOut>(
    `/agents/${encodeURIComponent(id)}/document`,
    { content },
  );
  refreshAll();
  return toAgent(dto);
}

/** Import an agent `.md` file. The result is always a draft. */
export async function importAgent(file: File): Promise<Agent> {
  const form = new FormData();
  form.append("file", file, file.name);
  const dto = await api.postForm<AgentOut>("/agents/import", form);
  refreshAll();
  return toAgent(dto);
}

/** Download the agent as `<name>.md`. */
export async function exportAgent(id: string, name: string): Promise<void> {
  await downloadContent(
    `/agents/${encodeURIComponent(id)}/export`,
    `${name}.md`,
  );
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
