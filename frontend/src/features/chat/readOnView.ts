/** Mark a thread's unread notifications read, skipping `except`; returns what it marked. */
export type MarkRead = (
  conversationId: string,
  except: ReadonlySet<string>,
) => string[];

/** Marks the thread on screen read, each id once per visit so one set back to unread stays
 *  unread; a hidden page marks nothing. Call it whenever the thread, visibility or list moves. */
export function createReadOnView(
  markRead: MarkRead,
): (openId: string | null, pageVisible: boolean) => void {
  let readFor: string | null = null;
  let marked = new Set<string>();
  return (openId, pageVisible) => {
    if (openId !== readFor) {
      readFor = openId;
      marked = new Set();
    }
    if (!openId || !pageVisible) return;
    for (const id of markRead(openId, marked)) marked.add(id);
  };
}
