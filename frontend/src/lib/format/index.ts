/** Formatters for consistent, tabular, diegetic-feeling output. */

/** Fixed-decimal number as a tabular string (e.g. 42.7). */
export function num(value: number, decimals = 1): string {
  return value.toFixed(decimals);
}

/** Zero-padded integer (e.g. 0341). */
export function pad(value: number, width = 4): string {
  return Math.trunc(value).toString().padStart(width, "0");
}

/** Percent with no decimals (e.g. 87%).
 *
 *  `digits` adds them back for a figure whose whole point is that it is small: rounding
 *  a 0.6% row to "1%" makes the smallest contributors indistinguishable from each other
 *  and from zero, which in a breakdown is the difference between "these are the rows
 *  that don't matter" and "these rows are broken". */
export function pct(value: number, digits = 0): string {
  return `${value.toFixed(digits)}%`;
}

/** A zone designator at the end of an ISO-8601 string: `Z`, or `±HH:MM`/`±HHMM`. */
const ZONED = /([Zz]|[+-]\d{2}:?\d{2})$/;

/** An ISO-8601 instant from the backend, as epoch milliseconds — `NaN` if unparseable.
 *
 *  **A timestamp with no zone designator is UTC, not local.** The backend stamps every
 *  datetime in UTC (`models/_fields.utcnow`), but SQLite has no datetime type, so a
 *  tz-aware value round-trips through the DB as a *naive* one; `core/serde.as_utc`
 *  re-stamps it on the way back out, and any route that reads a column without going
 *  through it serializes `2026-09-19T13:57:41.480578` — correct UTC, no `Z`.
 *
 *  `Date.parse` reads exactly that form as **local time**. West of Greenwich that puts
 *  every such timestamp in the future: on a UTC−4 host a thread created seconds ago
 *  parses four hours ahead, so an elapsed clock clamps to zero and a relative stamp
 *  reads "NOW" for four hours. Appending the `Z` the wire omitted is the whole fix.
 *
 *  Defensive on purpose rather than fixed once at the route that was caught doing it:
 *  the naive form is what a column read produces *by default*, so the next route to
 *  skip `as_utc` reintroduces it, silently and with no failing test. Normalizing here
 *  means every formatter in this module is right regardless.
 */
export function parseInstant(iso: string): number {
  if (!iso) return NaN;
  // A date-only string (`2026-09-19`) is already defined as UTC by the spec — only
  // the date-*time* form flips to local, so leave the short form alone.
  const needsZone = iso.includes("T") && !ZONED.test(iso);
  return Date.parse(needsZone ? `${iso}Z` : iso);
}

/** Mission elapsed time from milliseconds — fixed-width, zero-padded, every field
 *  always present (`00:14:22`, `04:21:07`, `132:00:09`).
 *
 *  Deliberately NOT `duration()` below, whose contract is the opposite one: coarsest
 *  useful unit, two units at most, human-readable. Both are right for their own job and
 *  neither can do the other's. A mission clock is read by *position* — the operator
 *  glances at the same three columns every time and sees which digits moved — so a
 *  representation that drops a field once it is zero, or switches units as it grows,
 *  destroys the only property that makes it scannable. It is also the one figure on
 *  screen that ticks, and a string that changes width while it ticks makes everything
 *  beside it jump.
 *
 *  Hours are not wrapped into days. A session running past 24h is a fact worth showing
 *  as `27:14:02` rather than hiding behind a day counter the operator then has to add
 *  back. Negative input clamps to zero — a clock that has not started reads `00:00:00`,
 *  never a minus sign, and clock skew between the host and the backend is not a state
 *  the interface should render as time travel. */
export function met(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(total % 60, 2)}`;
}

/** A duration from milliseconds, at the coarsest unit that still says something
 *  (e.g. `840ms`, `20.5s`, `39m44s`, `2h13m`).
 *
 *  Two units at most, and never a bare decimal above a minute: `39m44s` is read at a
 *  glance where `2384.2s` has to be divided first, and `39.7m` throws away the part a
 *  reader would then try to reconstruct. Below a minute the decimal is the useful
 *  digit — the difference between a 2.4s and a 2.9s first token is real — so seconds
 *  keep one, and only sub-second durations drop to whole milliseconds. */
export function duration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSeconds = ms / 1000;
  if (totalSeconds < 60) return `${totalSeconds.toFixed(1)}s`;
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes}m${Math.floor(totalSeconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h${(minutes % 60).toString().padStart(2, "0")}m`;
}

/** A large count abbreviated to three significant figures (e.g. `2.2M`, `450K`, `812`).
 *
 *  For counts that are read as a magnitude rather than a quantity — a thread's token
 *  total is "about two million", and the exact figure belongs in the tooltip.
 *
 *  `precise` keeps the tenth above 10K (`117.4K`, not `117K`). The default drops it
 *  because a lone magnitude is read, not compared — the digit is noise there. It is the
 *  opposite in a *breakdown*, where the figures are read against each other and rounding
 *  to the thousand makes neighbouring rows collide at the exact moment the operator is
 *  ranking them. Same rounding either way, one decision. */
export function compactCount(n: number, precise = false): string {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000 && !precise) return `${Math.round(n / 1000)}K`;
  if (abs >= 1_000) return `${(n / 1000).toFixed(1)}K`;
  return `${n}`;
}

/** A count and its noun, agreeing: `1 source`, `4 sources`, `0 topics`. `many` for the
 *  nouns that do not take an `s`. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Byte size in IEC units (e.g. 11.2 GB). */
export function bytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Coordinate pair to full precision (diegetic detail). */
export function coord(lat: number, lon: number): string {
  return `${lat.toFixed(7)} ${lon.toFixed(7)}`;
}

/** ISO timestamp -> compact UTC readout (e.g. 2026-06-07 14:32:05Z). */
export function timestamp(iso: string): string {
  const d = new Date(parseInstant(iso));
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number, w = 2) => n.toString().padStart(w, "0");
  return (
    `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}Z`
  );
}

/** Short date (e.g. 2026-06-07). */
export function date(iso: string): string {
  const d = new Date(parseInstant(iso));
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
}

/** Month names as a reader writes them, not as `Intl` abbreviates them.
 *
 *  `Intl`'s `short` month gives `Sep`; the convention this product writes is `Sept`,
 *  which is also the only abbreviation in the set that isn't simply the first three
 *  letters. Spelling the table out is what makes that deliberate rather than a locale
 *  accident — and it pins the output to one language, which matters for a stamp that
 *  sits in a fixed-width mono column beside eleven others. */
const MONTHS_SHORT = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "June",
  "July",
  "Aug",
  "Sept",
  "Oct",
  "Nov",
  "Dec",
];

const DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** English ordinal suffix for a day of the month — 1st, 2nd, 3rd, 11th, 21st. */
function ordinal(day: number): string {
  // The teens are the exception the naive mod-10 rule gets wrong: 11/12/13 take `th`
  // despite ending in 1/2/3.
  if (day >= 11 && day <= 13) return "th";
  switch (day % 10) {
    case 1:
      return "st";
    case 2:
      return "nd";
    case 3:
      return "rd";
    default:
      return "th";
  }
}

/** A turn's wall-clock stamp, written out — `Thu, Sept 10th 2026 @ 9:03PM`.
 *
 *  **Local time, unlike `timestamp()` above.** That one is a UTC readout for machine
 *  records, where a single absolute frame is the point. This one sits on a message in a
 *  conversation the operator had, so the useful question is "when was I here", and the
 *  answer has to be in the clock they were reading at the time.
 *
 *  Written out rather than relative (`5D AGO`), because a transcript is scrolled back
 *  through: a relative stamp answers "how long ago" for the turn under the cursor and
 *  becomes arithmetic for every turn above it, while a date answers the same question
 *  once and then stays true. It is still mono (§2 — a timestamp is emitted, not
 *  written), which is what keeps a long stamp ambient at `micro`. */
export function longTimestamp(iso: string): string {
  const d = new Date(parseInstant(iso));
  if (Number.isNaN(d.getTime())) return iso;
  const hours24 = d.getHours();
  // Midnight and noon are the two the modulo gets wrong — both land on 0, and a stamp
  // reading "0:03AM" is the kind of thing that makes the rest of the readout suspect.
  const hour = hours24 % 12 === 0 ? 12 : hours24 % 12;
  const minute = d.getMinutes().toString().padStart(2, "0");
  const meridiem = hours24 < 12 ? "AM" : "PM";
  const day = d.getDate();
  return (
    `${DAYS_SHORT[d.getDay()]}, ${MONTHS_SHORT[d.getMonth()]} ${day}${ordinal(day)} ` +
    `${d.getFullYear()} @ ${hour}:${minute}${meridiem}`
  );
}

/** Coarse relative time (e.g. 3M AGO, 2H AGO, IN 5D). Uppercase for labels. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = parseInstant(iso);
  if (Number.isNaN(then)) return iso;
  const delta = Math.round((now.getTime() - then) / 1000);
  const secs = Math.abs(delta);
  if (secs < 60) return "NOW";
  const label = (span: string) => (delta < 0 ? `IN ${span}` : `${span} AGO`);
  const mins = Math.round(secs / 60);
  if (mins < 60) return label(`${mins}M`);
  const hours = Math.round(mins / 60);
  if (hours < 24) return label(`${hours}H`);
  return label(`${Math.round(hours / 24)}D`);
}

/** A snake_case identifier as a phrase in sentence case — `tool_results` → "Tool
 *  results".
 *
 *  The fallback both of the interface's slug registries lean on: a tool's row label when
 *  the table has no entry for it, and a context contributor's row label when the override
 *  list doesn't name it. Neither can enumerate what the backend will grow next, so both
 *  have to read an unknown slug correctly from its own words — and when that was written
 *  twice the two copies had already drifted, one of them rendering `_` as an empty row.
 *
 *  `fallback` is for a caller that has a better thing to show than nothing when the slug
 *  is all punctuation — the tool table hands back the registry name it started from. */
export function sentenceCase(slug: string, fallback = slug): string {
  const words = slug.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : fallback;
}

/** The base domain of a URL — host minus a leading `www.` — as a compact label for
 *  "where does this go". Falls back to the raw string when it doesn't parse.
 *
 *  Shared by the two places that name a destination: the Sources row beneath an
 *  answer, and the hover label on an inline link the model wrote into its prose. */
export function hostLabel(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** A directory as the two segments worth reading: its own name, and the one above it.
 *
 *  `hostLabel`'s counterpart for the other kind of address. A directory is named by
 *  its last segment and by nothing else useful — but the operator has more than one
 *  `frontend`, and a heading that says only that names two different repositories
 *  identically. The parent is the smallest thing that tells them apart.
 *
 *  Two segments and never more: the rail is permanently on screen, and a full path
 *  across it spells out the operator's clients to anyone standing behind them.
 *  `parent` is empty at the filesystem root, where there is nothing above.
 *
 *  Separator-agnostic on purpose (`/` and `\` both) — the path is the host's, and this
 *  runs in a browser that knows nothing about which host produced it. */
export function pathLabel(path: string): { parent: string; name: string } {
  const segments = path.split(/[/\\]+/).filter(Boolean);
  return {
    parent: segments.length > 1 ? segments[segments.length - 2] : "",
    name: segments[segments.length - 1] ?? path,
  };
}
