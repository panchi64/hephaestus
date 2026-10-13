/** Schedule-builder helpers shared by the create/edit form and the routine list's
 *  human-readable schedule summary — extracted so the value<->seconds mapping
 *  and cron-shape hint live in exactly one place. */
import { parseInstant } from "~/lib/format";
import type { ScheduledTask, TaskSchedule } from "./model";

export type IntervalUnit = "seconds" | "minutes" | "hours" | "days";

export const UNIT_SECONDS: Record<IntervalUnit, number> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
};

/** Pick the largest whole unit an interval divides evenly into, for display —
 *  the inverse of the value+unit → seconds mapping the form submits. */
export function secondsToValueUnit(seconds: number): {
  value: string;
  unit: IntervalUnit;
} {
  const units: IntervalUnit[] = ["days", "hours", "minutes", "seconds"];
  for (const unit of units) {
    const size = UNIT_SECONDS[unit];
    if (seconds % size === 0) return { value: String(seconds / size), unit };
  }
  return { value: String(seconds), unit: "seconds" };
}

/** `datetime-local` <-> ISO. `datetime-local` has no timezone, so it's treated
 *  as the operator's local time (matching the native picker's own semantics). */
export function isoToLocalInput(iso: string): string {
  // The ISO side is a backend stamp, so it goes through `parseInstant` — read as
  // local, a zone-less one would put the picker hours off. The *other* direction
  // stays a bare `new Date` (`TaskFormModal`), because a `datetime-local` value
  // genuinely is wall-clock in the operator's zone.
  const d = new Date(parseInstant(iso));
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Client-hint-only shape check (five space-separated fields) — the backend is
 *  the validation authority; this is immediate UX feedback. */
export const CRON_PATTERN =
  /^[\d*,\-/]+\s+[\d*,\-/]+\s+[\d*,\-/]+\s+[\d*,\-/]+\s+[\d*,\-/]+$/;

/** One-line, human-readable schedule summary for the routine list row. */
export function humanizeSchedule(schedule: TaskSchedule): string {
  switch (schedule.type) {
    case "once":
      return schedule.runAt
        ? new Date(parseInstant(schedule.runAt)).toLocaleString()
        : "once";
    case "interval": {
      if (!schedule.everySeconds) return "interval";
      const { value, unit } = secondsToValueUnit(schedule.everySeconds);
      return `every ${value} ${unit}`;
    }
    case "cron":
      return schedule.cron
        ? `${schedule.cron}${schedule.timezone ? ` (${schedule.timezone})` : ""}`
        : "cron";
    case "webhook":
      return "webhook";
  }
}

/** The browser's IANA zone — the default a new schedule is read in. */
export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** An agent's draft that has not been approved never fires. */
export function isDraft(task: Pick<ScheduledTask, "approved">): boolean {
  return !task.approved;
}

/** Picker value for the default agent (stored as null). */
export const DEFAULT_AGENT_VALUE = "";

export const DEFAULT_AGENT_LABEL = "Default agent";

export function agentLabel(agentName: string | null): string {
  return agentName ?? DEFAULT_AGENT_LABEL;
}

/** Picker options: the default agent, then each name; a current agent that is no
 *  longer listed (unpublished since) stays selectable so an edit keeps it. */
export function agentOptions(
  names: readonly string[],
  current: string | null,
): { value: string; label: string }[] {
  const all =
    current !== null && !names.includes(current) ? [...names, current] : names;
  return [
    { value: DEFAULT_AGENT_VALUE, label: DEFAULT_AGENT_LABEL },
    ...all.map((name) => ({ value: name, label: name })),
  ];
}

/** The picker value to the stored `agentName`. */
export function agentFromValue(value: string): string | null {
  return value === DEFAULT_AGENT_VALUE ? null : value;
}
