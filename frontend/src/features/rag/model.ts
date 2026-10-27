/** Knowledge base (RAG) feature data contracts. */

import type { IconName } from "~/ui";

export type RagIndexStatus = "indexed" | "indexing" | "stale" | "error";

/** Where a source's content originates.
 *  - `surface`: an in-app corpus surface (uploads, memory, conversations) that is
 *    auto-indexed and managed where it lives. Not removable here — it's part of
 *    the system.
 *  - `folder`: an operator-added host path the server crawls and indexes.
 *    Removable. */
export type RagSourceKind = "surface" | "folder";

export interface RagSource {
  id: string;
  kind: RagSourceKind;
  /** Display name. For a `surface` it's the surface name ("Memory"); for a
   *  `folder` it's the host path. */
  label: string;
  /** Row icon — surfaces echo their nav icon; folders fall back to a path glyph. */
  icon: IconName;
  /** Surfaces link to where they're managed; null for one with no page of its own. */
  href?: string | null;
  docCount: number;
  status: RagIndexStatus;
  /** Null for surfaces that report no timestamp and folders not yet indexed. */
  lastIndexedAt: string | null;
  /** Short reason code for the last error, e.g. "PATH NOT FOUND". Phase 2 populated by backend. */
  errorHint?: string;
}

export interface RagIndexStats {
  /** Null when no embedding endpoint is configured (recall is keyword-only). */
  embeddingModel: string | null;
  /** Null until the active embedding model's dimensionality is known. */
  dims: number | null;
  /** Stored chunks across the chunked sources, not documents: memory and records
   *  surfaces never enter the chunk store. */
  totalDocs: number;
  totalCollections: number;
  storeSize: string;
}

/** Documents across every source, surfaces and folders alike. */
export function documentCount(sources: readonly RagSource[]): number {
  return sources.reduce((sum, source) => sum + source.docCount, 0);
}
