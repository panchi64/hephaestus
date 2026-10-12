/** How a context contributor reads on screen — the slug the backend sends becoming a
 *  glyph and a word.
 *
 *  The sibling of `toolPresentation.ts`, and for the same reason: the backend sends a
 *  registry slug (`skill_catalog`, `repo`, `plan`) because the wording of a readout is
 *  presentation and it has no business choosing sentence case, and two surfaces then need
 *  the same slug to become the same words. The context gauge's breakdown row and the work
 *  log's injection row are one block seen from two distances; naming it "Project
 *  instructions" in one and "Repo" in the other would leave the operator working out that
 *  they are the same thing.
 *
 *  **The overrides are a short list, not a registry.** Every tool category and every
 *  instruction provider the backend grows later reads correctly from its own id, so
 *  nothing here has to be edited when a feature ships one — only the few slugs whose own
 *  words would actively mislead are named. */

import { sentenceCase } from "~/lib/format";
import type { IconName } from "~/ui";

/** The few slugs whose de-slugged form would be wrong or unhelpful. */
const LABELS: Record<string, string> = {
  base: "Base prompt",
  external: "MCP & connectors",
  repo: "Project instructions",
  skill_catalog: "Skills",
  tasks: "Task list",
  date: "Today's date",
  workspace_posture: "Workspace posture",
  agent_brief: "Agent brief",
  // The thread's second axis beside its workspace, and the pair reads as a pair only if
  // the words match: "Level" alone names a scale without saying what is on it.
  level: "Permission posture",
  // An adjective is not a thing. What the block actually carries is the short index of
  // the tool groups the agent is *not* holding, which is what the noun has to say.
  dormant: "Dormant tools",
  delegate: "Delegates",
};

/** A backend slug as a row label: an override if it has one, else its own words
 *  (`tool_results` → "Tool results").
 *
 *  The de-slugging is `lib/format`'s, shared with the tool table's fallback — the two
 *  are the same rule read at two ranges, and the copy that lived here rendered a slug of
 *  nothing but underscores as a blank row. */
export function segmentLabel(id: string): string {
  return LABELS[id] ?? sentenceCase(id);
}

/** The one glyph every injection row leads with.
 *
 *  Deliberately **not** per-contributor, the way a tool row's glyph is per-family. A
 *  column of tool rows is scanned to tell the calls apart; a column of injection rows is
 *  scanned to tell them apart *from the calls*, and the fastest way to say "these are all
 *  the same kind of thing, and it isn't work" is one shape repeated. */
export const INJECTION_ICON: IconName = "inject";
