import type { RewindPreview } from "./data/rewind";

export type RestoreChoice = "restore" | "skip" | "cancel";

/** The token the send routes take to confirm a previewed rewind. */
export interface RestoreFiles {
  preview_token: string;
}

/** Whether the dry run names anything the operator should see before the leaf moves. */
export function hasRewindFiles(preview: RewindPreview): boolean {
  return (
    preview.restore.length +
      preview.delete.length +
      preview.conflicts.length +
      preview.unrestorable.length >
    0
  );
}

export interface RewindRow {
  key: string;
  path: string;
  projectId: string;
  mark: { label: string; tone: "dim" | "warn"; note?: string };
}

/** The dry run as the dialog lists it. Conflicts and files with no kept original are
 *  marked in `warn`: the rewind will not touch them. */
export function rewindRows(preview: RewindPreview): RewindRow[] {
  const plain = (
    files: { projectId: string; path: string }[],
    label: string,
    tone: "dim" | "warn",
    kind: string,
  ): RewindRow[] =>
    files.map((f) => ({
      key: `${kind}:${f.projectId}:${f.path}`,
      path: f.path,
      projectId: f.projectId,
      mark: { label, tone },
    }));
  return [
    ...plain(preview.restore, "Restore", "dim", "restore"),
    ...plain(preview.delete, "Delete", "dim", "delete"),
    ...preview.conflicts.map((c): RewindRow => ({
      key: `conflict:${c.projectId}:${c.path}`,
      path: c.path,
      projectId: c.projectId,
      mark: { label: "Conflict", tone: "warn", note: c.reason },
    })),
    ...plain(preview.unrestorable, "No original", "warn", "unrestorable"),
  ];
}

export type RestoreOutcome<T> = { sent: true; value: T } | { sent: false };

/**
 * Offer the file rewind in front of an edit or regenerate, then send.
 *
 * An empty dry run sends straight away. A stale token (the backend answers 409 when a
 * planned file changed since the preview) re-fetches the preview and asks again; a 409
 * whose fresh preview is unchanged is something else, such as a running turn, and is
 * rethrown for the caller to explain.
 */
export async function sendWithRestore<T>(deps: {
  fetchPreview: () => Promise<RewindPreview>;
  ask: (preview: RewindPreview, changed: boolean) => Promise<RestoreChoice>;
  send: (restore?: RestoreFiles) => Promise<T>;
  isConflict: (err: unknown) => boolean;
}): Promise<RestoreOutcome<T>> {
  let preview = await deps.fetchPreview();
  let changed = false;
  for (;;) {
    if (!hasRewindFiles(preview))
      return { sent: true, value: await deps.send() };
    const choice = await deps.ask(preview, changed);
    if (choice === "cancel") return { sent: false };
    if (choice === "skip") return { sent: true, value: await deps.send() };
    try {
      return {
        sent: true,
        value: await deps.send({ preview_token: preview.previewToken }),
      };
    } catch (err) {
      if (!deps.isConflict(err)) throw err;
      const fresh = await deps.fetchPreview();
      if (fresh.previewToken === preview.previewToken) throw err;
      preview = fresh;
      changed = true;
    }
  }
}
