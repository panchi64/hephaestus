/** Skills feature data contracts. A skill is an Agent Skills bundle (agentskills.io):
 *  a `SKILL.md` (frontmatter plus body) and supporting files. */

import type { Status } from "~/ui";

/** Where a skill came from; orthogonal to {@link Skill.published}. */
export type SkillSource = "authored" | "imported" | "agent";

/** A supporting file; `relpath` is its identity. */
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
  /** Named in every agent brief; an unpinned published skill is found by search.
   *  Only meaningful while published. */
  pinned: boolean;
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
  /** Recorded and displayed, never enforced: advisory only. */
  allowedTools: string[] | null;
  /** Non-standard frontmatter preserved verbatim so an export is lossless
   *  (an imported bundle's `when_to_use` lands here). Read-only in the UI. */
  extras: Record<string, unknown> | null;
  files: SkillFile[];
}

/* Display-only mirrors of the backend's limits: the backend decides and its 422
   message renders. Never gate a submit on these. */

export const SKILL_NAME_MAX = 64;
export const SKILL_DESCRIPTION_MAX = 1024;
/** Lowercase letters, numbers, and hyphens. */
export const SKILL_NAME_PATTERN = /^[a-z0-9-]+$/;

/** StatusFlag accent for published state, matching the document library's active/archived
 *  pair. A draft is `idle`, not `warn`: amber means caution system-wide. */
export function skillStatusFlag(published: boolean): Status {
  return published ? "nominal" : "idle";
}

/** What a pin means, for the toggle's tooltip. */
export const PIN_HINT =
  "Pinned skills are named in every conversation; others are found by search.";

/** Why the pin control is off for a draft. */
export const PIN_DRAFT_HINT =
  "Publish the skill first — a draft is never shown to Talos or your agents.";

export function skillStatusLabel(published: boolean): string {
  return published ? "PUBLISHED" : "DRAFT";
}

/** Provenance is a fact, not a state, so every source is neutral `idle`; the label text
 *  distinguishes them. `authored` renders no flag. */
export const skillSourceFlag: Record<SkillSource, Status> = {
  authored: "idle",
  imported: "idle",
  agent: "idle",
};

/** A read-only skill shipped with the app; `name` is its identity. */
export interface BuiltInSkill {
  name: string;
  description: string;
  body: string;
  enabled: boolean;
  /** A published stored skill of the same name is used instead. */
  shadowed: boolean;
  files: string[];
}

/** Why a shadowed built-in row is inert. */
export const SHADOWED_HINT =
  "A saved skill with this name is used instead of this one.";
