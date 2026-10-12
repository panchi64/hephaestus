/** Skills feature data contracts.
 *
 *  A skill is an **Agent Skills** bundle (agentskills.io): a `SKILL.md` — frontmatter
 *  plus a markdown body — and any supporting files shipped alongside it. The backend
 *  owns every rule below; the types here only describe what it sends back.
 */

import type { Status } from "~/ui";

/** Where a skill came from. Orthogonal to {@link Skill.published} — an imported
 *  skill is a draft until the operator publishes it, an authored one likewise. */
export type SkillSource = "authored" | "imported" | "agent";

/** One supporting file in the bundle. `relpath` is its identity. */
export interface SkillFile {
  relpath: string;
  sha256: string;
  sizeBytes: number;
}

/** A library-list row — no body, no bundle bytes. */
export interface SkillSummary {
  id: string;
  name: string;
  description: string;
  /** The trust boundary: only a published skill is visible to the agent. */
  published: boolean;
  source: SkillSource;
  fileCount: number;
  sizeBytes: number;
  createdAt: string;
  updatedAt: string;
}

export interface Skill extends SkillSummary {
  /** The `SKILL.md` body — everything below the frontmatter. */
  body: string;
  license: string | null;
  compatibility: string | null;
  metadata: Record<string, unknown> | null;
  /** Recorded and displayed, never enforced — advisory only (D32). */
  allowedTools: string[] | null;
  /** Non-standard frontmatter preserved verbatim so an export is lossless
   *  (an imported bundle's `when_to_use` lands here). Read-only in the UI. */
  extras: Record<string, unknown> | null;
  files: SkillFile[];
}

/* ── Presentation mirrors of the backend's rules ──────────────────────────────
   Character limits and the name charset are shown so the operator sees a field
   go red before they submit — the backend still decides, and its 422 message is
   what actually renders. Never gate a submit on these. */

export const SKILL_NAME_MAX = 64;
export const SKILL_DESCRIPTION_MAX = 1024;
/** Lowercase letters, numbers, and hyphens. */
export const SKILL_NAME_PATTERN = /^[a-z0-9-]+$/;

/** Maps a skill's published state to its StatusFlag accent — the same active /
 *  inactive pair the document library uses (`active: nominal`, `archived: idle`).
 *  A draft is **`idle`, not `warn`**: amber means caution system-wide, and an
 *  unpublished skill is merely inactive, not a problem. Keeping it neutral is also
 *  what stops a half-written library from reading as a screen full of warnings. */
export function skillStatusFlag(published: boolean): Status {
  return published ? "nominal" : "idle";
}

export function skillStatusLabel(published: boolean): string {
  return published ? "PUBLISHED" : "DRAFT";
}

/** Provenance is a *fact*, not a state, so it carries no accent: `imported` and
 *  `agent` would otherwise share one hue between two meanings, and every imported
 *  row would spend a second accent on something that isn't a signal. The label
 *  text alone distinguishes them — meaning is never in the hue. `authored` is the
 *  unremarkable case and renders no flag at all. */
export const skillSourceFlag: Record<SkillSource, Status> = {
  authored: "idle",
  imported: "idle",
  agent: "idle",
};
