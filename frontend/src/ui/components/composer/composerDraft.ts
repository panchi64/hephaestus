/* Self-contained guarded storage: the design system does not depend on ~/lib, so the
   Composer keeps its own best-effort draft persistence rather than importing the app's
   storage helper. Its own module so the feature layer can move a draft between keys
   (a new thread getting its id mid-run) without reaching into the Composer. */

const DRAFT_PREFIX = "ody.draft.";

/** The unsent draft stored under `key`, or "" when there is none (or no storage). */
export function loadDraft(key?: string): string {
  if (!key) return "";
  try {
    return localStorage.getItem(DRAFT_PREFIX + key) ?? "";
  } catch {
    return "";
  }
}

/** Persist `value` under `key`; an empty value removes the entry. */
export function saveDraft(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(DRAFT_PREFIX + key, value);
    else localStorage.removeItem(DRAFT_PREFIX + key);
  } catch {
    /* storage unavailable — drafts are best-effort */
  }
}

/** Carry the draft under `from` over to `to`, and clear `from`.
 *
 *  Only when there is something to carry and nowhere it would land on top of: a draft
 *  already under `to` is the operator's too, and silently replacing it with another
 *  would lose one of them. An empty `from` is a no-op, so a caller can call this on
 *  every bind without checking first. */
export function moveDraft(from: string, to: string): void {
  if (from === to) return;
  const text = loadDraft(from);
  if (!text.trim() || loadDraft(to)) return;
  saveDraft(to, text);
  saveDraft(from, "");
}
