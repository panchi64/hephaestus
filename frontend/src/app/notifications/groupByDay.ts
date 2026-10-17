import { parseInstant } from "~/lib/format";
import type { Notification } from "~/lib/stream/notificationEvents";

export type DayBucket = "Today" | "Yesterday" | "Earlier";

export interface DayGroup {
  label: DayBucket;
  items: Notification[];
}

const BUCKET_ORDER: DayBucket[] = ["Today", "Yesterday", "Earlier"];

function startOfDay(ms: number): number {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** Bucket by local calendar day; keeps input order within a bucket and omits empty buckets. */
export function groupByDay(
  items: readonly Notification[],
  now: Date,
): DayGroup[] {
  const today = startOfDay(now.getTime());
  const yesterday = startOfDay(today - 1);
  const buckets: Record<DayBucket, Notification[]> = {
    Today: [],
    Yesterday: [],
    Earlier: [],
  };
  for (const n of items) {
    const at = parseInstant(n.createdAt);
    if (at >= today) buckets.Today.push(n);
    else if (at >= yesterday) buckets.Yesterday.push(n);
    else buckets.Earlier.push(n);
  }
  return BUCKET_ORDER.filter((label) => buckets[label].length > 0).map(
    (label) => ({ label, items: buckets[label] }),
  );
}
