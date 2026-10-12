/** The inventory's folding rules.
 *
 *  Everything here is a rule that renders perfectly while being wrong, which is why it
 *  is tested rather than looked at: a fold that kept the wrong sighting shows a complete
 *  list of sources with the wrong shelf on each, and an origin count that deduped on the
 *  url instead of the host reports five sites where there is one.
 */

import { describe, expect, test } from "bun:test";
import { buildInventory, collectSources, type SourceItem } from "./sourceItems";
import type { ChatMessage, Citation } from "../model";

const turn = (...citations: Citation[]): ChatMessage =>
  ({
    id: `m-${citations.length}-${citations[0]?.key ?? "none"}`,
    role: "assistant",
    content: "",
    citations,
  }) as ChatMessage;

const web = (over: Partial<Citation> = {}): Citation => ({
  url: "https://example.com/a",
  key: "https://example.com/a",
  kind: "web",
  engagement: "listed",
  ...over,
});

describe("folding across the thread", () => {
  test("one source sighted twice is one row, and the count says so", () => {
    const items = collectSources([turn(web()), turn(web())]);
    expect(items).toHaveLength(1);
    expect(items[0].sightings).toBe(2);
  });

  test("the higher rung wins whichever turn it arrived in", () => {
    // The order matters: a naive "last write wins" passes the first case and fails the
    // second, and a naive "first write wins" does the reverse.
    const up = collectSources([
      turn(web({ engagement: "listed" })),
      turn(web({ engagement: "read" })),
    ]);
    expect(up[0].engagement).toBe("read");
    const down = collectSources([
      turn(web({ engagement: "read" })),
      turn(web({ engagement: "listed" })),
    ]);
    expect(down[0].engagement).toBe("read");
  });

  test("the sighting that knew a field is the one that supplies it", () => {
    // This is the real producer pattern: the search hit carries snippet and published,
    // the fetch that follows carries the higher rung and neither of those. Taking
    // either sighting wholesale loses half the row.
    const items = collectSources([
      turn(web({ snippet: "the passage", published: "March 2024" })),
      turn(web({ engagement: "read", retrievedAt: "2026-09-19T10:00:00Z" })),
    ]);
    expect(items[0]).toMatchObject({
      engagement: "read",
      snippet: "the passage",
      published: "March 2024",
      retrievedAt: "2026-09-19T10:00:00Z",
    });
  });

  test("a corpus passage folds by its own key, not by a null url", () => {
    const passage = (ref: string): Citation => ({
      url: null,
      key: `kb:${ref}`,
      kind: "corpus",
      engagement: "read",
      sourceId: "kb",
      ref,
    });
    // Two different passages out of one source. Keyed on `url` they would both be
    // `null` and fold into a single row — the defect the backend's `key` exists for.
    const items = collectSources([turn(passage("p1"), passage("p2"))]);
    expect(items).toHaveLength(2);
  });

  test("order is first sighting, oldest first", () => {
    const items = collectSources([
      turn(web({ url: "https://a.test/1", key: "a" })),
      turn(web({ url: "https://b.test/1", key: "b" })),
      turn(web({ url: "https://a.test/1", key: "a", engagement: "read" })),
    ]);
    expect(items.map((i) => i.key)).toEqual(["a", "b"]);
  });
});

describe("the figures at the top", () => {
  test("origins count publishers, not pages", () => {
    const inv = buildInventory(
      collectSources([
        turn(
          web({ url: "https://www.site.test/one", key: "1" }),
          web({ url: "https://site.test/two", key: "2" }),
          web({ url: "https://other.test/x", key: "3" }),
        ),
      ]),
    );
    // Three sources, two origins — and `www.` is not a fourth site.
    expect(inv.items).toHaveLength(3);
    expect(inv.originCount).toBe(2);
  });

  test("the newest retrieval is the latest, not the last one seen", () => {
    const inv = buildInventory(
      collectSources([
        turn(web({ key: "1", retrievedAt: "2026-09-19T12:00:00Z" })),
        turn(
          web({
            url: "https://b.test/",
            key: "2",
            retrievedAt: "2026-09-19T09:00:00Z",
          }),
        ),
      ]),
    );
    expect(inv.newestRetrieval).toBe("2026-09-19T12:00:00Z");
  });
});

describe("shelving", () => {
  const item = (over: Partial<SourceItem>): SourceItem => ({
    key: "k",
    kind: "web",
    url: "https://example.com/a",
    engagement: "read",
    origin: "example.com",
    sightings: 1,
    ...over,
  });

  test("a source is shelved by how far the run got with it", () => {
    for (const engagement of ["cited", "read", "listed"] as const)
      expect(
        buildInventory([item({ engagement })]).groups.map((g) => g.bucket),
      ).toEqual([engagement]);
  });

  test("a bucket with nothing in it is absent rather than empty", () => {
    // Nothing emits `cited` yet, so a groups list that always carried four entries
    // would print a permanently empty heading — the readout that teaches an operator
    // to stop reading headings.
    const inv = buildInventory([item({ engagement: "read" })]);
    expect(inv.groups.map((g) => g.bucket)).toEqual(["read"]);
  });

  test("groups read strongest first, whatever order the items arrived in", () => {
    const inv = buildInventory([
      item({ key: "a", engagement: "listed", url: "https://a.test/" }),
      item({ key: "b", engagement: "read", url: "https://b.test/" }),
      item({ key: "c", engagement: "cited", url: "https://c.test/" }),
    ]);
    expect(inv.groups.map((g) => g.bucket)).toEqual([
      "cited",
      "read",
      "listed",
    ]);
    expect(inv.groups[1].items.map((i) => i.key)).toEqual(["b"]);
  });
});
