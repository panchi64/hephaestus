/** Whose list is whose in the Tasks surface.
 *
 *  The rule is small and the failure it prevents is not: a sub-agent's steps merged into
 *  the launching thread's list read as one plan whose order makes no sense, and the
 *  progress count over the top of it counts parallel work as though it were sequential.
 */

import { describe, expect, test } from "bun:test";
import { flattenGroups, groupTasks } from "./taskGroups";
import type { TaskItem } from "~/lib/stream/events";

const task = (
  content: string,
  status: TaskItem["status"] = "pending",
): TaskItem => ({ id: content, content, status });

describe("grouping the task lists", () => {
  test("the thread's own list comes first and carries no heading", () => {
    const groups = groupTasks(
      [task("read it")],
      [{ handle: "helper-function-finder", tasks: [task("grep for it")] }],
    );
    expect(groups.map((g) => g.label)).toEqual([
      null,
      "helper-function-finder",
    ]);
    expect(groups[0].items.map((i) => i.content)).toEqual(["read it"]);
  });

  test("a heading leads with the callsign and class, and keeps the handle", () => {
    const groups = groupTasks(
      [],
      [
        {
          handle: "migrate-schema",
          callsign: "BRAX",
          agentClass: "Tripodes",
          tasks: [task("a")],
        },
        { handle: "old-row", callsign: "", agentClass: "", tasks: [task("b")] },
      ],
    );
    expect(groups.map((g) => g.label)).toEqual([
      "BRAX · Tripodes · migrate-schema",
      "old-row",
    ]);
  });

  test("a sub-agent with no list of its own is not an empty heading", () => {
    // "Has not written a list" and "has nothing to do" are the same state to the
    // operator, and a heading over nothing only asks a question.
    const groups = groupTasks(
      [task("read it")],
      [
        { handle: "test-runner", tasks: [] },
        { handle: "helper-function-finder", tasks: [task("grep for it")] },
      ],
    );
    expect(groups.map((g) => g.label)).toEqual([
      null,
      "helper-function-finder",
    ]);
  });

  test("a thread that delegated everything still has groups to show", () => {
    const groups = groupTasks(
      [],
      [{ handle: "test-runner", tasks: [task("run them")] }],
    );
    expect(groups.map((g) => g.label)).toEqual(["test-runner"]);
  });

  test("delegates keep the order they arrived in", () => {
    const groups = groupTasks(
      [],
      [
        { handle: "b", tasks: [task("b1")] },
        { handle: "a", tasks: [task("a1")] },
      ],
    );
    expect(groups.map((g) => g.label)).toEqual(["b", "a"]);
  });

  test("nothing anywhere is no groups", () => {
    expect(groupTasks([], [{ handle: "test-runner", tasks: [] }])).toEqual([]);
  });
});

describe("the progress count", () => {
  test("counts every list on screen, not just the thread's own", () => {
    const groups = groupTasks(
      [task("read it", "completed")],
      [
        {
          handle: "helper-function-finder",
          tasks: [task("grep for it", "completed"), task("report", "pending")],
        },
      ],
    );
    const all = flattenGroups(groups);
    expect(all).toHaveLength(3);
    expect(all.filter((i) => i.status === "completed")).toHaveLength(2);
  });
});
