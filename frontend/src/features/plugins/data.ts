import { createResource, createSignal, type Resource } from "solid-js";
import { api, downloadContent, isApiError } from "~/lib/api";
import {
  pluginDescription,
  type Plugin,
  type PluginFile,
  type PluginManifest,
  type PluginSecret,
  type PluginSource,
  type PluginTool,
  type PluginVersion,
  type Sensitivity,
} from "./model";

/* ── Backend DTO → seam type ──────────────────────────────────────────────── */

export interface ToolOut {
  name: string;
  description: string;
  sensitivity: Sensitivity;
  parameters: Record<string, unknown>;
}

export interface ManifestOut {
  description: string;
  requirements: string[];
  egress: string[];
  secrets: Record<string, string>;
  tools: ToolOut[];
  /** Absent from older payloads. */
  agents?: string[];
  skills?: string[];
  routines?: string[];
}

export interface PluginOut {
  id: string;
  name: string;
  source: PluginSource;
  published: boolean;
  active: ManifestOut | null;
  draft: ManifestOut | null;
  created_at: string;
  updated_at: string;
}

export interface PluginFileOut {
  path: string;
  content: string | null;
  size_bytes: number;
}

export interface PluginSecretOut {
  name: string;
  domain: string;
  is_set: boolean;
}

function toTool(dto: ToolOut): PluginTool {
  return {
    name: dto.name,
    description: dto.description,
    sensitivity: dto.sensitivity,
    parameters: dto.parameters,
  };
}

export function toManifest(dto: ManifestOut): PluginManifest {
  return {
    description: dto.description,
    requirements: dto.requirements,
    egress: dto.egress,
    secrets: dto.secrets,
    tools: dto.tools.map(toTool),
    agents: dto.agents ?? [],
    skills: dto.skills ?? [],
    routines: dto.routines ?? [],
  };
}

export function toPlugin(dto: PluginOut): Plugin {
  const active = dto.active ? toManifest(dto.active) : null;
  const draft = dto.draft ? toManifest(dto.draft) : null;
  return {
    id: dto.id,
    name: dto.name,
    description: pluginDescription(active, draft),
    source: dto.source,
    published: dto.published,
    active,
    draft,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
  };
}

export function toPluginFile(dto: PluginFileOut): PluginFile {
  return { path: dto.path, content: dto.content, sizeBytes: dto.size_bytes };
}

export function toPluginSecret(dto: PluginSecretOut): PluginSecret {
  return { name: dto.name, domain: dto.domain, isSet: dto.is_set };
}

const base = (id: string): string => `/plugins/${encodeURIComponent(id)}`;

export function filesPath(id: string, version: PluginVersion): string {
  return `${base(id)}/files?version=${version}`;
}

/* ── List ─────────────────────────────────────────────────────────────────── */

const [listTick, setListTick] = createSignal(0);
const [detailTick, setDetailTick] = createSignal(0);

function refreshAll(): void {
  setListTick((n) => n + 1);
  setDetailTick((n) => n + 1);
}

export function usePlugins(): Resource<Plugin[]> {
  const [data] = createResource(listTick, async () =>
    (await api.get<PluginOut[]>("/plugins")).map(toPlugin),
  );
  return data;
}

/* ── Detail ───────────────────────────────────────────────────────────────── */

async function fetchPlugin(id: string): Promise<Plugin | undefined> {
  try {
    return toPlugin(await api.get<PluginOut>(base(id)));
  } catch (err) {
    if (isApiError(err) && err.status === 404) return undefined;
    throw err;
  }
}

/** Resolves `undefined` for an unknown id. */
export function usePluginDetail(
  id: () => string,
): Resource<Plugin | undefined> {
  const [data] = createResource(
    () => [id(), detailTick()] as const,
    ([pluginId]) => fetchPlugin(pluginId),
  );
  return data;
}

/** Refetches the open plugin after a change made outside this module. */
export function refreshPluginDetail(): void {
  refreshAll();
}

export function usePluginFiles(
  id: () => string,
  version: () => PluginVersion,
): Resource<PluginFile[]> {
  const [data] = createResource(
    () => [id(), version(), detailTick()] as const,
    async ([pluginId, v]) =>
      (await api.get<PluginFileOut[]>(filesPath(pluginId, v))).map(
        toPluginFile,
      ),
  );
  return data;
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

async function act(path: string): Promise<Plugin> {
  const dto = await api.post<PluginOut>(path);
  refreshAll();
  return toPlugin(dto);
}

export function publishPlugin(id: string): Promise<Plugin> {
  return act(`${base(id)}/publish`);
}

export function unpublishPlugin(id: string): Promise<Plugin> {
  return act(`${base(id)}/unpublish`);
}

/** The plugin without its draft, or null when it had never been published and
 *  so was deleted with the draft. */
export async function discardDraft(id: string): Promise<Plugin | null> {
  const dto = await api.del<PluginOut | undefined>(`${base(id)}/draft`);
  refreshAll();
  return dto ? toPlugin(dto) : null;
}

export async function deletePlugin(id: string): Promise<void> {
  await api.del(base(id));
  refreshAll();
}

/** Import a zip. The result is always a draft. */
export async function importPlugin(file: File): Promise<Plugin> {
  const form = new FormData();
  form.append("file", file, file.name);
  const dto = await api.postForm<PluginOut>("/plugins/import", form);
  refreshAll();
  return toPlugin(dto);
}

/** Download the draft if there is one, else the active version, as a zip. */
export async function exportPlugin(
  id: string,
  name: string,
  version: "current" | "active" = "current",
): Promise<void> {
  await downloadContent(`${base(id)}/export?version=${version}`, `${name}.zip`);
}

/* ── Secrets ──────────────────────────────────────────────────────────────── */

const [secretsTick, setSecretsTick] = createSignal(0);

export function usePluginSecrets(id: () => string): Resource<PluginSecret[]> {
  const [data] = createResource(
    () => [id(), secretsTick()] as const,
    async ([pluginId]) =>
      (await api.get<PluginSecretOut[]>(`${base(pluginId)}/secrets`)).map(
        toPluginSecret,
      ),
  );
  return data;
}

export function refreshSecrets(): void {
  setSecretsTick((n) => n + 1);
}

/** The value is write-only; the backend never returns it. */
export async function setPluginSecret(
  id: string,
  name: string,
  value: string,
  domain?: string,
): Promise<void> {
  await api.put(
    `${base(id)}/secrets/${encodeURIComponent(name)}`,
    domain ? { value, domain } : { value },
  );
  refreshSecrets();
}

export async function deletePluginSecret(
  id: string,
  name: string,
): Promise<void> {
  await api.del(`${base(id)}/secrets/${encodeURIComponent(name)}`);
  refreshSecrets();
}

/* ── Errors ───────────────────────────────────────────────────────────────── */

export function pluginErrorMessage(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}
