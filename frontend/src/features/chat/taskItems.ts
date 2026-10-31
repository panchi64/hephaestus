import type { TaskItem } from "~/lib/stream";

/** The task list's headline numbers. Cancelled tasks leave the denominator: the question
 *  the count answers is "how much is left to do", and a cancelled task is not. */
export function taskSummary(items: TaskItem[]): {
  done: number;
  total: number;
  active: TaskItem | undefined;
} {
  return {
    done: items.filter((i) => i.status === "completed").length,
    total: items.filter((i) => i.status !== "cancelled").length,
    active: items.find((i) => i.status === "in_progress"),
  };
}

/** The harness stores an unset `active_form` as `""`, so a blank one falls back to the
 *  task's content. */
export function taskLabel(item: TaskItem): string {
  const running = item.status === "in_progress" && item.active_form?.trim();
  return running || item.content;
}
