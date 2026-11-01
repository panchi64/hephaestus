import { describe, expect, test } from "bun:test";
import { createReadOnView } from "./readOnView";

const setup = () => {
  const unread = { ids: ["n1"] };
  const calls: string[][] = [];
  const view = createReadOnView((_id, except) => {
    const ids = unread.ids.filter((n) => !except.has(n));
    calls.push(ids);
    return ids;
  });
  return { unread, calls, view };
};

describe("read-on-view", () => {
  test("marks the open thread's notifications read", () => {
    const { calls, view } = setup();
    view("c1", true);
    expect(calls).toEqual([["n1"]]);
  });

  test("a hidden page marks nothing until it is shown", () => {
    const { unread, calls, view } = setup();
    view("c1", false);
    unread.ids = ["n1", "n2"];
    view("c1", false);
    expect(calls).toEqual([]);

    view("c1", true);
    expect(calls).toEqual([["n1", "n2"]]);
  });

  test("an id is marked once per visit", () => {
    const { unread, calls, view } = setup();
    view("c1", true);
    unread.ids = ["n1", "n2"];
    view("c1", true);
    expect(calls.at(-1)).toEqual(["n2"]);

    view("c2", true);
    view("c1", true);
    expect(calls.at(-1)).toEqual(["n1", "n2"]);
  });

  test("no thread on screen marks nothing", () => {
    const { calls, view } = setup();
    view(null, true);
    expect(calls).toEqual([]);
  });
});
