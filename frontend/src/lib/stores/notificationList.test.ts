import { describe, expect, test } from "bun:test";
import type { Notification } from "~/lib/stream/notificationEvents";
import {
  reinsertRows,
  removeRows,
  restoreReadState,
  setReadState,
  upsertOrdered,
} from "./notificationList";

function make(
  id: string,
  minute: number,
  readAt: string | null = null,
): Notification {
  return {
    id,
    kind: "system",
    title: id,
    body: null,
    conversationId: null,
    runId: null,
    createdAt: new Date(2026, 9, 17, 12, minute).toISOString(),
    readAt,
    resolvedAt: null,
  };
}

const ids = (list: Notification[]) => list.map((n) => n.id);

describe("notificationList", () => {
  test("upsertOrdered files a new row in createdAt order and replaces a held one", () => {
    const list = [make("c", 30), make("a", 10)];
    const added = upsertOrdered(list, make("b", 20));
    expect(ids(added.items)).toEqual(["c", "b", "a"]);
    expect(added.previous).toBeUndefined();
    const replaced = upsertOrdered(list, make("a", 10, "x"));
    expect(ids(replaced.items)).toEqual(["c", "a"]);
    expect(replaced.previous?.readAt).toBeNull();
  });

  test("removeRows splits by id set or predicate", () => {
    const list = [make("a", 3), make("b", 2, "x"), make("c", 1)];
    expect(ids(removeRows(list, new Set(["a"])).items)).toEqual(["b", "c"]);
    expect(ids(removeRows(list, (n) => Boolean(n.readAt)).removed)).toEqual([
      "b",
    ]);
  });

  test("setReadState touches only rows whose state differs, and restore undoes it", () => {
    const list = [make("a", 3), make("b", 2, "x")];
    const { items, before } = setReadState(list, new Set(["a", "b"]), "now");
    expect(ids(before)).toEqual(["a"]);
    expect(items[0].readAt).toBe("now");
    expect(items[1].readAt).toBe("x");
    expect(restoreReadState(items, before)[0].readAt).toBeNull();
  });

  test("reinsertRows restores order and skips ids already held", () => {
    const list = [make("c", 3), make("a", 1)];
    const out = reinsertRows(list, [make("b", 2), make("a", 1)]);
    expect(ids(out)).toEqual(["c", "b", "a"]);
  });
});
