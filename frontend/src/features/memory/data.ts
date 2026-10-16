import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type { DuplicateGroup, Memory, RecallHit } from "./model";

/* ── Backend DTOs → seam types ────────────────────────────────────────────── */

interface MemoryOut {
  id: string;
  content: string;
  description: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  has_embedding: boolean;
}

interface DuplicateGroupOut {
  memory_ids: string[];
  similarity: number;
}

interface RecallHitOut {
  id: string;
  content: string;
  matched_by: string;
  score: number;
}

function toMemory(dto: MemoryOut): Memory {
  return {
    id: dto.id,
    content: dto.content,
    description: dto.description,
    pinned: dto.pinned,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
    hasEmbedding: dto.has_embedding,
  };
}

/* ── List (the seam) ──────────────────────────────────────────────────────── */

const [memTick, setMemTick] = createSignal(0);

async function fetchMemories(): Promise<Memory[]> {
  const rows = await api.get<MemoryOut[]>("/memory");
  return rows.map(toMemory);
}

export function useMemories(): Resource<Memory[]> {
  const [data] = createResource(memTick, fetchMemories);
  return data;
}

/** Invalidate the list after a mutation. */
export function refreshMemories(): void {
  setMemTick((n) => n + 1);
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

export async function addMemory(draft: {
  content: string;
  description: string;
  pinned: boolean;
}): Promise<void> {
  await api.post("/memory", draft);
  refreshMemories();
}

export async function updateMemory(
  id: string,
  patch: { content?: string; description?: string; pinned?: boolean },
): Promise<void> {
  await api.patch(`/memory/${id}`, patch);
  refreshMemories();
}

export async function deleteMemory(id: string): Promise<void> {
  await api.del(`/memory/${id}`);
  refreshMemories();
}

/* ── Recall + audit (on demand) ───────────────────────────────────────────── */

export async function recall(query: string, limit = 5): Promise<RecallHit[]> {
  const hits = await api.post<RecallHitOut[]>("/memory/recall", {
    query,
    limit,
  });
  return hits.map((h) => ({
    id: h.id,
    content: h.content,
    matchedBy: h.matched_by,
    score: h.score,
  }));
}

/** Run the dedup audit and resolve each group's member ids to full memories. */
export async function auditDuplicates(
  memories: Memory[],
): Promise<DuplicateGroup[]> {
  const groups = await api.post<DuplicateGroupOut[]>("/memory/audit");
  const byId = new Map(memories.map((m) => [m.id, m]));
  return groups.map((g) => ({
    similarity: g.similarity,
    memories: g.memory_ids
      .map((id) => byId.get(id))
      .filter((m): m is Memory => m !== undefined),
  }));
}
