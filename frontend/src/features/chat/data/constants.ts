/**
 * The literals the chat seam agrees on with the backend.
 *
 * They live apart from the mappers because unrelated layers need them and importing one
 * of those from another just to reach a string would tie modules together for no other
 * reason.
 *
 * *Which tools render as a terminal* used to live here, as one tool name the cold
 * mapper, the live fold and the terminal card each compared against. It is a property of
 * a tool rather than a fact about the seam, and there is more than one such tool, so it
 * is `ToolEntry.terminal` in `toolPresentation.ts` now — one table, asked once per site.
 */

/** The prompt a "Continue." turn sends — the operator's way to resume a turn that a
 *  bound (inactivity timeout or cancel) stopped before it finished. A plain
 *  user turn on the same conversation, so the model picks up where the prior turn left
 *  off and a small "Continue." bubble appears in the transcript. */
export const CONTINUE_PROMPT = "Continue.";
