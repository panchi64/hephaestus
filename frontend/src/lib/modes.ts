import type { IconName } from "~/ui";

/**
 * What kind of work a thread is — the backend's `services/modes.py` vocabulary, sent
 * verbatim on the send that creates the conversation and never translated on the way.
 *
 * **It lives in `lib/` rather than in `features/chat/` because two layers outside chat
 * need it.** The theme layer binds the signature accent to it (`ui/theme/accents.ts`),
 * and the app shell stamps it on the document root. A type owned by a feature and
 * imported by the design system would be a dependency pointing the wrong way, and
 * declaring the union twice would be worse: the two copies would drift the first time a
 * fourth mode arrives, and nothing would fail.
 *
 * `normal` (shown to the operator as "Chat" — the wire value never changes) and
 * `research` work in the conversation's own sandbox container; `code` works
 * in a git worktree of a project's repository on the operator's machine.
 */
export type SessionMode = "normal" | "research" | "code";

/** Where a mode's threads do their work. The distinction the interface keeps asking
 *  about — a `worktree` thread is cut from a directory on the operator's machine, so it
 *  needs one named before it can start, it files under that directory in the rail, and
 *  its branch is the thing the status strip reports. A `sandbox` thread has none of
 *  that. Three surfaces used to ask by comparing the id to `"code"`, which said which
 *  mode rather than what about it, and would have to be found again the day a fourth
 *  mode is also rooted in a directory. */
export type ModeWorkspace = "sandbox" | "worktree";

export interface SessionModeSpec {
  id: SessionMode;
  /** Sentence case — the interface naming the thing to the operator. */
  label: string;
  /** What the mode changes, said in one line: the workspace and the tools. */
  description: string;
  icon: IconName;
  workspace: ModeWorkspace;
  /** Whether a finished answer in this mode is read back against its own sources.
   *
   *  **A mirror of `services/modes.py`, and it has to move with it.** The backend owns
   *  the rule — the pass costs a background model call per answer, so only a mode whose
   *  whole output is a synthesis of things it read declares it — and the flag is not on
   *  the wire. The interface needs it anyway: the control that asks for a reading has to
   *  know whether asking can produce one. Without it a normal thread that happened to
   *  run a web search offers the check, the POST does no model call and answers with the
   *  same empty list, and the button sits there forever looking broken. */
  attributesClaims: boolean;
}

/** By id — the declaration, and what `sessionModeSpec` looks up. Keyed rather than a
 *  list searched by id, so the lookup is total over the union: a `find` needs a
 *  fallback, and a fallback is a mode silently resolving to a different one's rules. */
const SPECS = {
  normal: {
    id: "normal",
    label: "Chat",
    description: "General work in this thread's own sandbox.",
    icon: "chat",
    workspace: "sandbox",
    attributesClaims: false,
  },
  research: {
    id: "research",
    label: "Research",
    description: "Reading the web and the corpus, with sources cited.",
    icon: "research",
    workspace: "sandbox",
    attributesClaims: true,
  },
  code: {
    id: "code",
    label: "Code",
    description: "A git worktree of a directory on this machine.",
    icon: "code",
    workspace: "worktree",
    attributesClaims: false,
  },
} satisfies Record<SessionMode, SessionModeSpec>;

/** The three, in the order the rail lists them — the declaration order above. Ordinary
 *  work first: it is what most threads are, and a switch whose default sits in the
 *  middle reads as a spectrum. */
export const SESSION_MODES: readonly SessionModeSpec[] = Object.values(SPECS);

export const SESSION_MODE_IDS: readonly SessionMode[] = SESSION_MODES.map(
  (spec) => spec.id,
);

/** The mode a thread is when nothing says otherwise — matching the backend's
 *  `DEFAULT_MODE`, and the one that reaches the least. */
export const DEFAULT_SESSION_MODE: SessionMode = "normal";

export function isSessionMode(value: string): value is SessionMode {
  return (SESSION_MODE_IDS as readonly string[]).includes(value);
}

/** Whatever the wire said, as a mode this build has a rule for. A row written by
 *  another build degrades to the mode that reaches the least rather than to a section
 *  of the rail the operator cannot open. */
export function sessionMode(value: string | undefined): SessionMode {
  return value && isSessionMode(value) ? value : DEFAULT_SESSION_MODE;
}

/** Everything this build knows about a mode, in one lookup. Total by construction —
 *  there is no "unknown mode" arm to hide a missing row behind, because a value that
 *  isn't one of the three never had this type. Whatever came off the wire goes through
 *  `sessionMode()` first. */
export function sessionModeSpec(mode: SessionMode): SessionModeSpec {
  return SPECS[mode];
}
