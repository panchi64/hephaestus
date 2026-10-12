import { parseInstant } from "~/lib/format";
import type { ChatSummary } from "./model";

/**
 * How the rail arranges its threads.
 *
 * Two kinds of thread want two kinds of heading, and the split is what there is to file
 * *under*. A thread scoped to a project has a directory, and after a fortnight of them
 * the flat list is a hundred rows whose titles all sound alike; what the operator is
 * looking for is "the threads in *this* repository", which is a heading, not a search. A
 * thread with no project has no directory at all, so the only heading that means
 * anything for it is *when*.
 *
 * That second heading replaced a per-row `3D AGO` stamp, and the trade is the point:
 * the stamp answered "when" once per row, in a column the eye had to visit for every
 * one of them, to separate threads that were mostly from the same week anyway. A
 * heading answers it once for a run and then stays out of the way.
 *
 * So this is one pass that files each thread by what it has, and the two kinds of
 * heading are genuinely different rather than one shape wearing different text — see
 * `SessionGroup`. Pure: ordering is the caller's (it reads the pin store), and the pin
 * set and the clock arrive as arguments, which is what keeps this testable without a DOM
 * or a fixed today.
 */

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
  /** The heading. Always present: every run the rail draws says what it is. */
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
  /** The project behind the heading — what the section's own controls act on. */
  projectId: string;
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

/** What the grouping needs from the caller: the directories project threads file under,
 *  the clock the rest file by, and the pins that lift a run out in front of the clock. */
export interface GroupingContext {
  /** The directories the operator works in. */
  directories?: readonly WorkspaceDirectory[];
  /** Which threads the operator has kept to hand. Only a thread with no project is
   *  lifted out by a pin; a project's threads stay under their directory. */
  pinned?: ReadonlySet<string>;
  /** Injectable clock — the bucket boundaries are the whole behaviour, so a test has
   *  to be able to stand at a known moment. */
  now?: Date;
}

const NO_PINS: ReadonlySet<string> = new Set();

/**
 * Partition an **already ordered** list into the sections the rail shows.
 *
 * A thread with a project files under that project's directory; every other thread files
 * by when it was last touched. Groups that hold threads appear in the order their first
 * thread does, so the input's ordering carries all the way through: a pinned thread
 * floats its whole workspace to the top, and otherwise the most recently touched
 * repository leads. That is one rule rather than two, and it means the rail never
 * reorders itself for a reason the operator cannot see in the rows.
 *
 * `directories` is what the operator works in, whether or not any of it has been worked
 * on yet — a thread is started from its directory, so a directory with no threads is the
 * normal beginning of a piece of work rather than an empty row. Those trail the ones
 * holding threads, in the order given (the projects listing, already most-recently-opened
 * first): a section with nothing in it has nothing to say about how recently the
 * operator was there.
 *
 * Filing is by **project id**, not by name. Two directories can be called `frontend`,
 * and filing by name would merge two repositories into one section whose controls would
 * then act on whichever won.
 *
 * A thread whose project is not among the directories is **left out**. That is the
 * archived case, and dropping it is the point: archiving a directory is the operator
 * saying they no longer work there, and a rail that hid the heading while keeping ten of
 * its threads loose in the list would have honoured the letter of that and none of the
 * intent. Nothing is lost — pointing at the directory again un-archives it and the
 * threads come back under it. Callers must not render until the directory listing has
 * resolved, or every filed thread would vanish for the width of a fetch.
 *
 * Pins come out into their own run rather than floating to the top of `Today`, where
 * they were indistinguishable from anything else touched today. `orderSessions` has
 * already floated them, so this only has to name what it finds. A bucket with nothing in
 * it is never created, so the rail never draws a heading with nothing under it.
 */
export function groupSessions(
  sessions: ChatSummary[],
  context: GroupingContext = {},
): SessionGroup[] {
  const directories = context.directories ?? [];
  const pinned = context.pinned ?? NO_PINS;
  const now = context.now ?? new Date();
  const named = new Map(directories.map((d) => [d.id, d.label]));
  const groups: SessionGroup[] = [];
  const byId = new Map<string, SessionGroup>();

  const section = (group: SessionGroup): SessionGroup => {
    const existing = byId.get(group.id);
    if (existing) return existing;
    byId.set(group.id, group);
    groups.push(group);
    return group;
  };

  const file = (s: ChatSummary): SessionGroup | undefined => {
    if (s.projectId) {
      const label = named.get(s.projectId);
      return label === undefined
        ? undefined
        : section({
            kind: "directory",
            id: s.projectId,
            label,
            projectId: s.projectId,
            sessions: [],
          });
    }
    const label = pinned.has(s.id)
      ? PINNED_GROUP
      : recencyLabel(s.updatedAt, now);
    return section({ kind: "recency", id: label, label, sessions: [] });
  };

  for (const { session, anchor } of nestUnderParents(sessions))
    file(anchor)?.sessions.push(session);

  for (const d of directories)
    section({
      kind: "directory",
      id: d.id,
      label: d.label,
      projectId: d.id,
      sessions: [],
    });
  return groups;
}

/** The list with every thread that names a parent present in it moved directly beneath
 *  that parent, depth-first. Each entry carries the top-level thread it hangs from — the
 *  one whose project, pin and recency decide the section — so a thread opened from an
 *  agent tree files with its parent and not by its own stamps. */
function nestUnderParents(
  sessions: ChatSummary[],
): { session: ChatSummary; anchor: ChatSummary }[] {
  const ids = new Set(sessions.map((s) => s.id));
  const children = new Map<string, ChatSummary[]>();
  const top: ChatSummary[] = [];
  for (const s of sessions) {
    if (s.parentId && s.parentId !== s.id && ids.has(s.parentId)) {
      const siblings = children.get(s.parentId);
      if (siblings) siblings.push(s);
      else children.set(s.parentId, [s]);
    } else top.push(s);
  }
  const out: { session: ChatSummary; anchor: ChatSummary }[] = [];
  const seen = new Set<string>();
  const walk = (s: ChatSummary, anchor: ChatSummary) => {
    if (seen.has(s.id)) return;
    seen.add(s.id);
    out.push({ session: s, anchor });
    for (const child of children.get(s.id) ?? []) walk(child, anchor);
  };
  for (const s of top) walk(s, s);
  for (const s of sessions) walk(s, s);
  return out;
}

/** How many threads above this one, within the list it is shown in. */
export function nestingDepth(
  session: ChatSummary,
  inList: readonly ChatSummary[],
): number {
  const byId = new Map(inList.map((s) => [s.id, s]));
  let depth = 0;
  let at = session;
  while (at.parentId && depth < inList.length) {
    const parent = byId.get(at.parentId);
    if (!parent) break;
    depth += 1;
    at = parent;
  }
  return depth;
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
