import { parseInstant } from "~/lib/format";
import { sessionModeSpec, type SessionMode } from "~/lib/modes";
import type { ChatSummary } from "./model";

/**
 * How the rail arranges a mode's threads.
 *
 * The three modes do not want the same list, and the split is what there is to file
 * *under*. A Code thread has a directory, and after a fortnight of them the flat list
 * is a hundred rows whose titles all sound alike; what the operator is looking for is
 * "the threads in *this* repository", which is a heading, not a search. Normal and
 * Research threads work in their own sandbox and have no directory at all — so the
 * only heading that means anything for them is *when*.
 *
 * That second heading replaced a per-row `3D AGO` stamp, and the trade is the point:
 * the stamp answered "when" once per row, in a column the eye had to visit for every
 * one of them, to separate threads that were mostly from the same week anyway. A
 * heading answers it once for a run and then stays out of the way.
 *
 * So this is a strategy over one input rather than two list components, and the two
 * strategies produce genuinely different headings rather than one shape wearing
 * different text — see `SessionGroup`. Pure: ordering is the caller's (it reads the
 * pin store), and the pin set and the clock arrive as arguments, which is what keeps
 * this testable without a DOM or a fixed today.
 */

/** The heading a code thread lands under when its project is gone — deleting a
 *  project unfiles its conversations rather than deleting them, so this run is
 *  real and has to be reachable. Matches the projects vocabulary, where *unfiled*
 *  means visible everywhere rather than orphaned. */
export const UNFILED_GROUP = "Unfiled";

/** The heading for threads the operator has kept to hand. It leads the list, because
 *  the whole point of a pin is not to go looking. */
export const PINNED_GROUP = "Pinned";

/** Where a thread lands once it is past the buckets that count in days. */
export const OLDER_GROUP = "Older";

/** One directory the operator works in, as the rail needs it: the project it is, and
 *  what to call it. The caller resolves the name (`pathLabel`) — this file decides
 *  where divisions fall and nothing about how they read. */
export interface WorkspaceDirectory {
  id: string;
  label: string;
}

interface GroupBase {
  /** Stable identity for keying and for remembering which sections are open. */
  id: string;
  /** The heading. Always present: every run the rail draws now says what it is. */
  label: string;
  sessions: ChatSummary[];
}

/**
 * A directory's threads. The heading is a **place** — it folds, it remembers whether
 * it is open, and it carries the controls that act on that directory (start a thread
 * here, archive it).
 */
export interface DirectoryGroup extends GroupBase {
  kind: "directory";
  /** The project behind the heading, or null for `Unfiled`. What the section's own
   *  controls act on — none of which `Unfiled` has, because there is no directory
   *  behind it. */
  projectId: string | null;
}

/**
 * A run of threads last touched in the same stretch of time. The heading is a
 * **caption** — it names the run and does nothing else.
 *
 * Deliberately not a `DirectoryGroup` with its controls left null. A recency bucket is
 * not somewhere the operator goes, so it has nothing to start a thread *in* and nowhere
 * to navigate; and it must not fold, because the fold state is stored per heading and
 * defaults every section but one to shut — which on `Today` would hide the list the
 * operator just asked for. Those are three different facts about the same object, and
 * a `collapsible` flag beside a nullable `projectId` would have left the render to
 * rediscover them one `&&` at a time.
 */
export interface RecencyGroup extends GroupBase {
  kind: "recency";
}

export type SessionGroup = DirectoryGroup | RecencyGroup;

/** What the strategies need from the caller. Each mode reads its own half: a worktree
 *  mode files under `directories`, a sandbox mode under the clock, and `pinned` lifts
 *  a run out in front of both. */
export interface GroupingContext {
  /** The directories a worktree mode files threads under. */
  directories?: readonly WorkspaceDirectory[];
  /** Which threads the operator has kept to hand. */
  pinned?: ReadonlySet<string>;
  /** Injectable clock — the bucket boundaries are the whole behaviour, so a test has
   *  to be able to stand at a known moment. */
  now?: Date;
}

const NO_PINS: ReadonlySet<string> = new Set();

/**
 * Partition an **already ordered** list into the sections this mode shows.
 *
 * Groups that hold threads appear in the order their first thread does, so the input's
 * ordering carries all the way through: a pinned thread floats its whole workspace to
 * the top, and otherwise the most recently touched repository leads. That is one rule
 * rather than two, and it means the rail never reorders itself for a reason the
 * operator cannot see in the rows.
 */
export function groupSessions(
  sessions: ChatSummary[],
  mode: SessionMode,
  context: GroupingContext = {},
): SessionGroup[] {
  // Having a workspace is what gives a mode directories to file under; everything else
  // is filed by when it was last touched.
  return sessionModeSpec(mode).workspace === "worktree"
    ? byDirectory(sessions, context.directories ?? [])
    : byRecency(sessions, context.pinned ?? NO_PINS, context.now ?? new Date());
}

/**
 * File a worktree mode's threads under the directory each works in.
 *
 * `directories` is what the operator works in, whether or not any of it has been worked
 * on yet — the directory now comes *first* and the thread is started from it, so a
 * directory with no threads is the normal beginning of a piece of work rather than an
 * empty row. Those trail the ones holding threads, in the order given (the projects
 * listing, already most-recently-opened first): a section with nothing in it has
 * nothing to say about how recently the operator was there.
 *
 * Filing is by **project id**, not by name. Two directories can be called `frontend`,
 * and filing by name would merge two repositories into one section whose controls would
 * then act on whichever won.
 */
function byDirectory(
  sessions: ChatSummary[],
  directories: readonly WorkspaceDirectory[],
): DirectoryGroup[] {
  const named = new Map(directories.map((d) => [d.id, d.label]));
  const groups: DirectoryGroup[] = [];
  const byId = new Map<string, DirectoryGroup>();

  const section = (id: string, label: string, projectId: string | null) => {
    let group = byId.get(id);
    if (!group) {
      group = { kind: "directory", id, label, projectId, sessions: [] };
      byId.set(id, group);
      groups.push(group);
    }
    return group;
  };

  for (const s of sessions) {
    // Unfiled: the thread's project is gone (deleting one unfiles its conversations
    // rather than deleting them), so there is no directory to file it under.
    if (!s.projectId) {
      section(UNFILED_GROUP, UNFILED_GROUP, null).sessions.push(s);
      continue;
    }
    // A thread whose project *exists* but isn't among the directories is **left out**,
    // not swept into Unfiled. That is the archived case, and dropping it is the point:
    // archiving a directory is the operator saying they no longer work there, and a
    // rail that hid the heading while keeping ten of its threads loose in the list
    // would have honoured the letter of that and none of the intent. Nothing is lost —
    // pointing at the directory again un-archives it and the threads come back under
    // it. Callers must not render until the directory listing has resolved, or every
    // filed thread would vanish for the width of a fetch.
    const label = named.get(s.projectId);
    if (label !== undefined)
      section(s.projectId, label, s.projectId).sessions.push(s);
  }

  for (const d of directories) section(d.id, d.label, d.id);
  return groups;
}

/**
 * File a sandbox mode's threads by when they were last touched.
 *
 * Pins come out into their own run rather than floating to the top of `Today`, where
 * they were indistinguishable from anything else touched today. `orderSessions` has
 * already floated them, so this only has to name what it finds.
 *
 * Like the directory strategy, a bucket appears where its first thread does and one
 * with nothing in it is never created — so the input's ordering carries through and the
 * rail never draws a heading with nothing under it.
 */
function byRecency(
  sessions: ChatSummary[],
  pinned: ReadonlySet<string>,
  now: Date,
): RecencyGroup[] {
  const groups: RecencyGroup[] = [];
  const byLabel = new Map<string, RecencyGroup>();

  for (const s of sessions) {
    const label = pinned.has(s.id)
      ? PINNED_GROUP
      : recencyLabel(s.updatedAt, now);
    let group = byLabel.get(label);
    if (!group) {
      group = { kind: "recency", id: label, label, sessions: [] };
      byLabel.set(label, group);
      groups.push(group);
    }
    group.sessions.push(s);
  }
  return groups;
}

/** Local midnight `daysBack` days before `d`.
 *
 *  Every boundary goes through here rather than subtracting multiples of 86,400,000ms
 *  from one timestamp, and that is not pedantry: a day is not always 24 hours. In the
 *  week after a daylight-saving change the arithmetic version lands on 23:00 or 01:00
 *  of the boundary day, so a thread touched just after midnight falls on the wrong side
 *  and is filed a whole bucket away. "Yesterday evening" is not "24 hours ago", and
 *  asking the calendar is the only way to say so. */
function startOfDay(d: Date, daysBack = 0): number {
  return new Date(
    d.getFullYear(),
    d.getMonth(),
    d.getDate() - daysBack,
  ).getTime();
}

/** Which run a thread falls in. The named buckets cover the last month, where the
 *  operator thinks in days; past that they think in months, so that is what the heading
 *  becomes. */
function recencyLabel(updatedAt: string, now: Date): string {
  const t = parseInstant(updatedAt);
  // An unparseable stamp sorts with the oldest rather than crashing the rail. It
  // cannot be trusted into a bucket that claims to know when it was.
  if (Number.isNaN(t)) return OLDER_GROUP;
  if (t >= startOfDay(now)) return "Today";
  if (t >= startOfDay(now, 1)) return "Yesterday";
  if (t >= startOfDay(now, 7)) return "Previous 7 days";
  if (t >= startOfDay(now, 30)) return "Previous 30 days";
  const then = new Date(t);
  // Same calendar year gets the bare month; anything older carries the year, so
  // `March` can never mean two different Marches in one list.
  const month = then.toLocaleString("en-US", { month: "long" });
  return then.getFullYear() === now.getFullYear()
    ? month
    : `${month} ${then.getFullYear()}`;
}

/** How long since a thread last moved, as its rail row's readout: `now`, `2m`, `4h`,
 *  `1d`. **Only inside the last 30 days** — past that the group heading already names
 *  the month, and an age in the hundreds of days is a number nobody reads. Undefined
 *  then, and for a stamp that does not parse. */
export function rowAge(
  updatedAt: string,
  now: Date = new Date(),
): string | undefined {
  const t = parseInstant(updatedAt);
  if (Number.isNaN(t)) return undefined;
  const mins = Math.floor((now.getTime() - t) / 60_000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return days < 30 ? `${days}d` : undefined;
}
