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

/** The detail the backend gives a confirmed rewind whose token no longer matches. A
 *  conversation-busy 409 carries a different one. */
const STALE_REWIND_DETAIL = "since the rewind was previewed";

export function isStaleRewind(err: unknown): boolean {
  if (typeof err !== "object" || err === null) return false;
  const { status, detail } = err as { status?: unknown; detail?: unknown };
  return (
    status === 409 &&
    typeof detail === "string" &&
    detail.includes(STALE_REWIND_DETAIL)
  );
}

/**
 * Offer the file rewind in front of an edit or regenerate, then send.
 *
 * An empty dry run sends straight away. A stale-rewind 409 re-fetches the preview and
 * asks again; any other error, a busy conversation included, is rethrown for the caller.
 * A preview that cannot be read sends untouched on a 404 (nothing tracks this thread's
 * files) and otherwise asks whether to go on without checking.
 */
export async function sendWithRestore<T>(deps: {
  fetchPreview: () => Promise<RewindPreview>;
  ask: (preview: RewindPreview, changed: boolean) => Promise<RestoreChoice>;
  send: (restore?: RestoreFiles) => Promise<T>;
  isMissing: (err: unknown) => boolean;
  confirmUnchecked: () => Promise<boolean>;
}): Promise<RestoreOutcome<T>> {
  let preview: RewindPreview;
  try {
    preview = await deps.fetchPreview();
  } catch (err) {
    if (!deps.isMissing(err) && !(await deps.confirmUnchecked()))
      return { sent: false };
    return { sent: true, value: await deps.send() };
  }
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
      if (!isStaleRewind(err)) throw err;
      preview = await deps.fetchPreview();
      changed = true;
    }
  }
}
