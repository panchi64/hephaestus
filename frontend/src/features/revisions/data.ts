import { createSignal } from "solid-js";
import { api } from "~/lib/api";

/* ── Backend DTOs → seam types ────────────────────────────────────────────── */

export type ChangeStatus = "added" | "removed" | "changed";

export interface Revision {
  number: number;
  note: string;
  sizeBytes: number;
  createdAt: string;
}

export interface FileChange {
  path: string;
  status: ChangeStatus;
  /** Unified diff; null for a binary file. */
  patch: string | null;
}

export interface RevisionOut {
  number: number;
  note: string;
  size_bytes: number;
  created_at: string;
}

export interface FileChangeOut {
  path: string;
  status: ChangeStatus;
  patch: string | null;
}

export function toRevision(dto: RevisionOut): Revision {
  return {
    number: dto.number,
    note: dto.note,
    sizeBytes: dto.size_bytes,
    createdAt: dto.created_at,
  };
}

export function toFileChange(dto: FileChangeOut): FileChange {
  return { path: dto.path, status: dto.status, patch: dto.patch };
}

/** Added and removed line counts of a unified diff's hunks. */
export function patchStats(patch: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  let inHunk = false;
  for (const line of patch.split("\n")) {
    if (line.startsWith("@@")) inHunk = true;
    else if (!inHunk) continue;
    else if (line.startsWith("+")) added++;
    else if (line.startsWith("-")) removed++;
  }
  return { added, removed };
}

/* ── Requests ─────────────────────────────────────────────────────────────── */

/** Bumped whenever history may have changed; the history view refetches. */
const [tick, setTick] = createSignal(0);

export const revisionsTick = tick;

export function refreshRevisions(): void {
  setTick((n) => n + 1);
}

/** `kind` is the collection the item lives in; `id` is the item's id. */
export function revisionsBase(kind: "skills" | "agents", id: string): string {
  return `/${kind}/${encodeURIComponent(id)}/revisions`;
}

export async function listRevisions(base: string): Promise<Revision[]> {
  return (await api.get<RevisionOut[]>(base)).map(toRevision);
}

/** How the item now differs from its latest published version. */
export async function getUnpublishedChanges(
  base: string,
): Promise<FileChange[]> {
  return (await api.get<FileChangeOut[]>(`${base}/changes`)).map(toFileChange);
}

/** How the item now differs from version `n`; empty when identical. */
export async function getRevisionDiff(
  base: string,
  n: number,
): Promise<FileChange[]> {
  return (await api.get<FileChangeOut[]>(`${base}/${n}`)).map(toFileChange);
}

export async function restoreRevision(base: string, n: number): Promise<void> {
  await api.post<void>(`${base}/${n}/restore`);
  refreshRevisions();
}
