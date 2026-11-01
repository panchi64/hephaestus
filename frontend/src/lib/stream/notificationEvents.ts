/**
 * Client mirror of the backend's notification surface (`backend/models/notification.py`
 * and its SSE envelope). A separate stream from the per-run union in `./events.ts`; a
 * shape change here must be matched in the backend.
 */

/** A reminder task fires `reminder` directly (title = task title, body = the prompt
 *  verbatim); any task whose output channel is `notification` also fires `task_outcome`
 *  at terminal with a short outcome summary. */
export type NotificationKind =
  | "approval_needed"
  | "run_completed"
  | "run_failed"
  | "save_failed"
  | "vm_down"
  | "reminder"
  | "task_outcome"
  | "system";

/** `NotificationOut`: the shape of both `GET /notifications` items and the SSE
 *  envelope's `notification` field (camelCase). */
export interface Notification {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string | null;
  conversationId: string | null;
  runId: string | null;
  createdAt: string;
  readAt: string | null;
  /** Set when what it is about resolves (an approval decided by any path, or its run
   *  reaching terminal). Independent of `readAt`. */
  resolvedAt: string | null;
}

interface Base {
  seq: number;
  ts: string;
}

export interface NotificationCreated extends Base {
  type: "notification.created";
  notification: Notification;
}

/** Read/resolved state changed, including a read made from another tab or session. */
export interface NotificationUpdated extends Base {
  type: "notification.updated";
  notification: Notification;
}

/** `notification` is its last state before dismissal or clearing. */
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
