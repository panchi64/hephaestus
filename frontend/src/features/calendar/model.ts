/** Calendar feature data contracts. */

/** The repetition choices the picker offers, plus `custom` — the read-only bucket for a
 *  rule the backend holds that the picker can't express (an imported or CalDAV-synced
 *  series). It reads as "repeats", never as "none", so a recurring event is never shown
 *  as one-off. */
export type RecurrenceRule =
  "none" | "daily" | "weekly" | "monthly" | "weekdays" | "custom";

/** What the picker can actually produce. `custom` is display-only — a rule the backend
 *  holds and the picker can't express — so it is never something the operator can pick. */
export type RecurrenceChoice = Exclude<RecurrenceRule, "custom">;

export interface Calendar {
  id: string;
  name: string;
  /** Color expressed as a TextTone accent for status chips. */
  tone: "nominal" | "info" | "warn" | "alert";
  synced: boolean;
  syncUrl?: string;
}

export interface CalendarEvent {
  id: string;
  calendarId: string;
  title: string;
  start: string; // ISO datetime
  end: string; // ISO datetime
  location?: string;
  /** How the event repeats, for display in the detail panel. */
  recurrence?: RecurrenceRule;
  /** Whether this is one instance of a series — the backend's own answer, which is what
   *  decides whether removing it cancels an occurrence or deletes the event. Never
   *  inferred from `recurrence`: only the backend knows what it stored. */
  recurring: boolean;
  allDay?: boolean;
  description?: string;
}

export type CalendarMarkPattern = "filled" | "hollow" | "hatched" | "crossed";

const MARK_PATTERN: Record<Calendar["tone"], CalendarMarkPattern> = {
  nominal: "filled",
  info: "hollow",
  warn: "hatched",
  alert: "crossed",
};

/** The grayscale fill pattern that identifies a calendar in place of a hue. */
export function calendarMarkPattern(
  tone: Calendar["tone"],
): CalendarMarkPattern {
  return MARK_PATTERN[tone];
}
