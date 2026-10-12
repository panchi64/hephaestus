import { createResource, createSignal, type Resource } from "solid-js";
import { api, downloadContent, isApiError } from "~/lib/api";
import type { Skill, SkillFile, SkillSource, SkillSummary } from "./model";

/* ── Backend DTOs → seam types ────────────────────────────────────────────── */

interface SkillFileOut {
  relpath: string;
  sha256: string;
  sizeBytes: number;
}

interface SkillSummaryOut {
  id: string;
  name: string;
  description: string;
  published: boolean;
  source: string;
  fileCount: number;
  sizeBytes: number;
  createdAt: string;
  updatedAt: string;
}

interface SkillOut extends Omit<SkillSummaryOut, "fileCount" | "sizeBytes"> {
  body: string;
  license?: string | null;
  compatibility?: string | null;
  metadata?: Record<string, unknown> | null;
  allowedTools?: string[] | null;
  extras?: Record<string, unknown> | null;
  files: SkillFileOut[];
}

interface SkillImportOut {
  skill: SkillOut;
  warnings: string[];
}

/** The wire's open `source` string narrowed to the three the model knows. An
 *  unrecognized value is treated as authored — the least-claiming provenance. */
function toSource(value: string): SkillSource {
  return value === "imported" || value === "agent" ? value : "authored";
}

function toFile(dto: SkillFileOut): SkillFile {
  return {
    relpath: dto.relpath,
    sha256: dto.sha256,
    sizeBytes: dto.sizeBytes,
  };
}

function toSummary(dto: SkillSummaryOut): SkillSummary {
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description,
    published: dto.published,
    source: toSource(dto.source),
    fileCount: dto.fileCount,
    sizeBytes: dto.sizeBytes,
    createdAt: dto.createdAt,
    updatedAt: dto.updatedAt,
  };
}

/** The detail response has no `fileCount`/`sizeBytes` of its own — it ships the
 *  file list, so the two summary fields are read off it rather than refetching
 *  the library row. */
function toSkill(dto: SkillOut): Skill {
  const files = dto.files.map(toFile);
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description,
    published: dto.published,
    source: toSource(dto.source),
    fileCount: files.length,
    sizeBytes: files.reduce((total, f) => total + f.sizeBytes, 0),
    createdAt: dto.createdAt,
    updatedAt: dto.updatedAt,
    body: dto.body,
    license: dto.license ?? null,
    compatibility: dto.compatibility ?? null,
    metadata: dto.metadata ?? null,
    allowedTools: dto.allowedTools ?? null,
    extras: dto.extras ?? null,
    files,
  };
}

/* ── List (the seam) ──────────────────────────────────────────────────────── */

const [listTick, setListTick] = createSignal(0);

async function fetchSkills(): Promise<SkillSummary[]> {
  // Pull drafts too — the directory's tabs filter what's already loaded, and a
  // draft is precisely what the operator came here to review and publish.
  const rows = await api.get<SkillSummaryOut[]>("/skills?published_only=false");
  return rows.map(toSummary);
}

export function useSkills(): Resource<SkillSummary[]> {
  const [data] = createResource(listTick, fetchSkills);
  return data;
}

/** Invalidate the library list after a mutation. */
export function refreshSkills(): void {
  setListTick((n) => n + 1);
}

/* ── Detail (the seam) ────────────────────────────────────────────────────── */

const [detailTick, setDetailTick] = createSignal(0);

async function fetchSkill(id: string): Promise<Skill | undefined> {
  try {
    return toSkill(await api.get<SkillOut>(`/skills/${id}`));
  } catch (err) {
    // An unknown id is a state the editor renders (NOT FOUND), not a failure to
    // throw at Suspense. Anything else still is.
    if (isApiError(err) && err.status === 404) return undefined;
    throw err;
  }
}

/** Single skill for the editor. Resolves `undefined` for an unknown id. */
export function useSkillDetail(id: () => string): Resource<Skill | undefined> {
  const [data] = createResource(
    () => [id(), detailTick()] as const,
    ([skillId]) => fetchSkill(skillId),
  );
  return data;
}

/** Invalidate the open skill's detail after a mutation. */
export function refreshSkillDetail(): void {
  setDetailTick((n) => n + 1);
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

/** Everything `PATCH /skills/{id}` accepts. Request bodies are snake_case (only
 *  responses are camelCase), so `allowedTools` is renamed on the way out. */
export interface SkillPatch {
  name?: string;
  description?: string;
  body?: string;
  license?: string;
  compatibility?: string;
  metadata?: Record<string, unknown>;
  allowedTools?: string[];
}

export async function createSkill(
  name: string,
  description: string,
  body = "",
): Promise<Skill> {
  const dto = await api.post<SkillOut>("/skills", { name, description, body });
  refreshSkills();
  return toSkill(dto);
}

export async function updateSkill(
  id: string,
  patch: SkillPatch,
): Promise<Skill> {
  const { allowedTools, ...rest } = patch;
  const dto = await api.patch<SkillOut>(`/skills/${id}`, {
    ...rest,
    ...(allowedTools !== undefined ? { allowed_tools: allowedTools } : {}),
  });
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

/** Replace one exact span of the body instead of rewriting it whole. Rejects
 *  with a 409 when `oldText` isn't unique — the caller renders that verbatim. */
export async function editSkillSpan(
  id: string,
  oldText: string,
  newText: string,
): Promise<Skill> {
  const dto = await api.patch<SkillOut>(`/skills/${id}/span`, {
    old_text: oldText,
    new_text: newText,
  });
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

export async function deleteSkill(id: string): Promise<void> {
  await api.del(`/skills/${id}`);
  refreshSkills();
}

/** Cross the trust boundary in either direction. Publishing is its own endpoint
 *  precisely because it is not a field edit. */
export async function setSkillPublished(
  id: string,
  published: boolean,
): Promise<Skill> {
  const dto = await api.post<SkillOut>(
    `/skills/${id}/${published ? "publish" : "unpublish"}`,
  );
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

/** Import a `.zip` bundle or a bare `SKILL.md`. The result is always a draft;
 *  `warnings` is what the operator should know before publishing it. */
export async function importSkill(
  file: File,
): Promise<{ skill: Skill; warnings: string[] }> {
  const form = new FormData();
  form.append("file", file, file.name);
  const dto = await api.postForm<SkillImportOut>("/skills/import", form);
  refreshSkills();
  return { skill: toSkill(dto.skill), warnings: dto.warnings };
}

/** Download the skill as an Agent Skills bundle. Goes through the client (not a
 *  bare anchor) because the export endpoint is bearer-authed. */
export async function exportSkill(id: string, name: string): Promise<void> {
  await downloadContent(`/skills/${id}/export`, `${name}.zip`);
}

/* ── Document (the whole SKILL.md) ────────────────────────────────────────── */

/** The full `SKILL.md` — frontmatter and body — as the backend renders it. */
export async function getSkillDocument(id: string): Promise<string> {
  const doc = await api.get<{ content: string }>(`/skills/${id}/document`);
  return doc.content;
}

/** Replace the skill from an edited `SKILL.md`. The backend validates it as an
 *  import would be; a rejection is a 422 naming the field. */
export async function putSkillDocument(
  id: string,
  content: string,
): Promise<Skill> {
  const dto = await api.put<SkillOut>(`/skills/${id}/document`, { content });
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

/* ── Bundle files ─────────────────────────────────────────────────────────── */

/** A file's path is its identity and may contain `/`, so encode per segment
 *  rather than encoding the whole thing (which would escape the separators). */
function filePath(id: string, relpath: string): string {
  const encoded = relpath.split("/").map(encodeURIComponent).join("/");
  return `/skills/${id}/files/${encoded}`;
}

/** Add or replace a supporting file. Returns the skill with its new file list. */
export async function putSkillFile(
  id: string,
  relpath: string,
  file: File,
): Promise<Skill> {
  const form = new FormData();
  form.append("file", file, file.name);
  const dto = await api.putForm<SkillOut>(filePath(id, relpath), form);
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

/** A bundle file's text. */
export async function getSkillFileText(
  id: string,
  relpath: string,
): Promise<string> {
  return (await api.getBlob(filePath(id, relpath))).text();
}

export async function deleteSkillFile(
  id: string,
  relpath: string,
): Promise<Skill> {
  const dto = await api.del<SkillOut>(filePath(id, relpath));
  refreshSkills();
  refreshSkillDetail();
  return toSkill(dto);
}

export async function downloadSkillFile(
  id: string,
  relpath: string,
): Promise<void> {
  await downloadContent(
    filePath(id, relpath),
    relpath.split("/").pop() ?? relpath,
  );
}

/* ── Errors ───────────────────────────────────────────────────────────────── */

/** The backend's message for a failure, verbatim — it decides what's wrong and
 *  how to say it. `fallback` covers a transport failure, which produced no
 *  message at all. */
export function skillErrorMessage(err: unknown, fallback: string): string {
  return isApiError(err) ? err.detail : fallback;
}

/** The input a 422 blamed, when it named one, so a form can attach the message
 *  to that control instead of only toasting it. */
export function skillErrorField(err: unknown): string | null {
  return isApiError(err) ? (err.field ?? null) : null;
}
