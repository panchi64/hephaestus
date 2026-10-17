/**
 * The notification protocol — the client mirror of the backend's notification
 * surface (`backend/models/notification.py` + its SSE envelope).
 *
 * This is a SEPARATE, independently-versioned stream from the per-run event
 * union in `./events.ts` (that union is frozen and untouched by this file).
 * Keep this in lockstep with the backend the same way `events.ts` mirrors
 * `backend/runs/events/` — any shape change here must be matched there, and
 * vice versa.
 */

/** `reminder` and `task_outcome` are the scheduler/tasks surface's two kinds —
 *  a reminder task fires `reminder` directly (title = task title, body = the
 *  prompt verbatim); any task whose output channel is `notification` also
 *  fires `task_outcome` at terminal with a short outcome summary. */
export type NotificationKind =
  | "approval_needed"
  | "run_completed"
  | "run_failed"
  | "reminder"
  | "task_outcome"
  | "system";

/** `NotificationOut` from the REST/SSE wire contract. Both `GET /notifications`
 *  items and the SSE envelope's `notification` field use this exact shape
 *  (camelCase). */
export interface Notification {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string | null;
  conversationId: string | null;
  runId: string | null;
  createdAt: string;
  /** Set once the operator has read it. */
  readAt: string | null;
  /** Set when the thing it's about is resolved (e.g. an approval decided,
   *  by any path — approve/deny/conversation-grant — or its run reaching
   *  terminal). Distinct from `readAt`: a resolved notification can still be
   *  unread. */
  resolvedAt: string | null;
}

interface Base {
  seq: number;
  ts: string;
}

/** A new notification was created. */
export interface NotificationCreated extends Base {
  type: "notification.created";
  notification: Notification;
}

/** An existing notification changed — read/resolved state, incl. read-elsewhere
 *  sync (marked read from another tab/session). */
export interface NotificationUpdated extends Base {
  type: "notification.updated";
  notification: Notification;
}

/** A notification was removed (dismissed or cleared); `notification` is its last state. */
export interface NotificationDeleted extends Base {
  type: "notification.deleted";
  notification: Notification;
}

export type NotificationStreamEvent =
  NotificationCreated | NotificationUpdated | NotificationDeleted;

/** REST backfill response shape (`GET /notifications`). */
export interface NotificationsPage {
  items: Notification[];
  unreadCount: number;
}
