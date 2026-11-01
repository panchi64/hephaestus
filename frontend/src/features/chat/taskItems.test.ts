import { describe, expect, test } from "bun:test";
import type { TaskItem } from "~/lib/stream";
import { taskLabel, taskSummary } from "./taskItems";

const item = (over: Partial<TaskItem>): TaskItem => ({
  id: "t1",
  content: "Write the report",
  status: "pending",
  ...over,
});

describe("taskLabel", () => {
  test("a running task shows its running form", () => {
    expect(
      taskLabel(
        item({ status: "in_progress", active_form: "Writing the report" }),
      ),
    ).toBe("Writing the report");
  });

  test("a running task with a blank running form falls back to its content", () => {
    expect(taskLabel(item({ status: "in_progress", active_form: "" }))).toBe(
      "Write the report",
    );
    expect(taskLabel(item({ status: "in_progress", active_form: null }))).toBe(
      "Write the report",
    );
  });

  test("a task that isn't running shows its content", () => {
    expect(
      taskLabel(item({ status: "completed", active_form: "Writing" })),
    ).toBe("Write the report");
  });
});

describe("taskSummary", () => {
  test("cancelled tasks leave the total, and the running one is active", () => {
    const running = item({ id: "t2", status: "in_progress" });
    expect(
      taskSummary([
        item({ status: "completed" }),
        running,
        item({ id: "t3", status: "cancelled" }),
      ]),
    ).toEqual({ done: 1, total: 2, active: running });
  });
});
