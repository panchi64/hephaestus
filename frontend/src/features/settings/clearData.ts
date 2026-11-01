import { plural } from "~/lib/format";

export type DataKind = "chats" | "memories" | "notifications" | "uploads";

export interface ClearResultDTO {
  deleted: number;
  skipped_ids: string[];
}

export interface ClearResult {
  deleted: number;
  /** Chats a live run held, left in place. */
  skippedIds: string[];
}

export function toClearResult(dto: ClearResultDTO): ClearResult {
  return { deleted: dto.deleted, skippedIds: dto.skipped_ids };
}

const NOUNS: Record<DataKind, [one: string, many?: string]> = {
  chats: ["chat"],
  memories: ["memory", "memories"],
  notifications: ["notification"],
  uploads: ["upload"],
};

export function clearedMessage(kind: DataKind, result: ClearResult): string {
  const deleted = `Deleted ${plural(result.deleted, ...NOUNS[kind])}.`;
  const skipped = result.skippedIds.length;
  return skipped === 0
    ? deleted
    : `${deleted} ${skipped} still running, left in place.`;
}

/** Whether the open thread went in a chats clear: every chat goes except the skipped. */
export function openThreadCleared(
  openId: string | null,
  result: ClearResult,
): boolean {
  return openId !== null && !result.skippedIds.includes(openId);
}
