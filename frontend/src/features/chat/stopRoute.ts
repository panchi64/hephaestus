import { isLive, type Subagent } from "./data/subagents";

/** What Stop does: cancel the thread's own run, which also stops its agents; stop the
 *  agents of a thread with no run; or nothing, when nothing is live. */
export type StopRoute = "run" | "agents" | "none";

export function stopRoute(state: {
  sending: boolean;
  subagents: readonly Subagent[];
}): StopRoute {
  if (state.sending) return "run";
  return state.subagents.some(isLive) ? "agents" : "none";
}
