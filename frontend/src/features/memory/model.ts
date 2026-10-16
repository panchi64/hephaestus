/** Memory feature data contracts. Mirrors the backend `/memory/*` surface —
 *  single-operator long-term memory: content, description, pinned, timestamps, and
 *  whether an embedding exists. There is no "type"/category in the backend model. */

export interface Memory {
  id: string;
  content: string;
  /** The one-line index entry Talos sees for a pinned memory. The backend fills a
   *  blank one with the content's first line. */
  description: string;
  pinned: boolean;
  createdAt: string;
  updatedAt: string;
  /** True once a dense embedding has been computed (else keyword-only recall). */
  hasEmbedding: boolean;
}

/** A near-duplicate cluster from the dedup audit. The operator resolves it by
 *  deleting redundant members (the backend has no merge — audit only detects). */
export interface DuplicateGroup {
  memories: Memory[];
  similarity: number;
}

/** One hit from hybrid recall (`/memory/recall`). */
export interface RecallHit {
  id: string;
  content: string;
  /** How it matched: "semantic" | "keyword" | "both". */
  matchedBy: string;
  score: number;
}
