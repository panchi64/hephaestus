import { api } from "~/lib/api";

export interface RewindFile {
  projectId: string;
  path: string;
}

export interface RewindConflict extends RewindFile {
  reason: string;
}

/** What putting the files back to before a turn would do. A preview writes nothing. */
export interface RewindPreview {
  restore: RewindFile[];
  delete: RewindFile[];
  /** Left alone because they changed since the turns last wrote them. */
  conflicts: RewindConflict[];
  /** Too large for their original to have been kept. */
  unrestorable: RewindFile[];
  /** Names the planned set and what each file holds now. */
  previewToken: string;
}

export async function fetchRewindPreview(
  conversationId: string,
  messageId: string,
): Promise<RewindPreview> {
  return api.get<RewindPreview>(
    `/conversations/${conversationId}/changes/rewind?messageId=${encodeURIComponent(messageId)}`,
  );
}
