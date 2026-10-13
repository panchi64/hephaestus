/**
 * The thread list — one shared reading of it, and the two rules that reorder it.
 *
 * The chat room and the nav rail's RECENTS both render this list. A resource per surface
 * would double-fetch and, worse, drift: one would still be showing yesterday's activity
 * edge while the other had refetched. So the resource is a singleton under its own
 * never-disposed root, and `refreshSessions()` is the one way anything asks for a re-read.
 *
 * Ordering lives here rather than in a component for the same reason the resource does:
 * pins are the operator's own arrangement of the list, they are read by every surface that
 * renders it, and a component that sorted its own copy would disagree with the one beside
 * it the moment a pin was toggled.
 */

import {
  createResource,
  createRoot,
  createSignal,
  type Accessor,
} from "solid-js";
import { api } from "~/lib/api";
import { parseInstant } from "~/lib/format";
import { settled } from "~/lib/resource";
import { readLS, writeLS } from "~/lib/storage";
import type { ChatSummary } from "../model";
import { toSummary } from "./summaries";
import type { ConversationSummaryDTO } from "./wire";

/* ── Recency-gated resume ─────────────────────────────────────────────────────
   On entry the chat resumes the last conversation only while it's still "warm"
   (last activity within the window); otherwise it opens a fresh composer. */

export const RESUME_WINDOW_MS = 15 * 60 * 1000;

export function isWarm(iso: string, now = Date.now()): boolean {
  // `parseInstant`, not `new Date` — a zone-less backend stamp read as local time
  // lands in the future west of Greenwich, which makes `now - t` negative and every
  // thread warm forever, so the window silently stopped expiring.
  const t = parseInstant(iso);
  return !Number.isNaN(t) && now - t <= RESUME_WINDOW_MS;
}

/** The session to land on at entry: the newest warm thread, or null = start
 *  fresh. Assumes `list` is newest-first (as the seam returns it). */
export function entrySessionId(list: ChatSummary[]): string | null {
  const warm = list.find((s) => isWarm(s.updatedAt));
  return warm ? warm.id : null;
}

/* ── Pinned threads (non-recency ordering) ────────────────────────────────── */

const PINS_KEY = "heph.chat.pins";
function readPins(): Set<string> {
  try {
    const raw = readLS(PINS_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}
const [_pinned, _setPinned] = createSignal<Set<string>>(readPins());
export const pinnedIds = _pinned;
export function isPinned(id: string): boolean {
  return _pinned().has(id);
}
export function togglePin(id: string): void {
  const next = new Set(_pinned());
  if (next.has(id)) next.delete(id);
  else next.add(id);
  _setPinned(next);
  writeLS(PINS_KEY, JSON.stringify([...next]));
}

/** Pinned threads first (recency preserved within each group). */
export function orderSessions(list: ChatSummary[]): ChatSummary[] {
  const pins = _pinned();
  if (pins.size === 0) return list;
  const pinned = list.filter((s) => pins.has(s.id));
  const rest = list.filter((s) => !pins.has(s.id));
  return [...pinned, ...rest];
}

/* ── The list itself ──────────────────────────────────────────────────────── */

let refetchSessions: (() => void) | undefined;
let sessionsAccessor: Accessor<ChatSummary[] | undefined> | undefined;

async function fetchSessions(): Promise<ChatSummary[]> {
  const rows = await api.get<ConversationSummaryDTO[]>("/conversations");
  return rows.map(toSummary);
}

/** The app-wide conversation list — one shared resource read by both the chat
 *  room and the nav rail's RECENTS. A singleton (under its own never-disposed
 *  root, like `mainChat`) so the two surfaces can't double-fetch or drift, and so
 *  `refreshSessions()` after a turn updates the single list both render. */
export function useChatSessions(): Accessor<ChatSummary[] | undefined> {
  if (sessionsAccessor) return sessionsAccessor;
  return (sessionsAccessor = createRoot(() => {
    const [data, { refetch }] = createResource(fetchSessions);
    refetchSessions = refetch;
    // Read `settled`, not the resource itself. `refreshSessions()` runs after every
    // turn and refetches in place; reading the resource under the app's
    // fallback-less root <Suspense> would re-suspend it for the duration of each
    // refetch, blanking the whole page for a frame. `settled` keeps the prior list
    // on screen while the refetch is in flight, so a finishing stream no longer
    // flickers the page — and before the first read it is `undefined` rather than a
    // suspension, which is what the rail's loading arm is for.
    return () => settled(data);
  }));
}

/** Re-read the conversation list (after a turn, rename, or delete). */
export function refreshSessions(): void {
  refetchSessions?.();
}
