import { describe, expect, test } from "bun:test";
import type { RewindPreview } from "./data/rewind";
import {
  hasRewindFiles,
  isStaleRewind,
  rewindRows,
  sendWithRestore,
  type RestoreChoice,
} from "./rewindRestore";

const preview = (over: Partial<RewindPreview> = {}): RewindPreview => ({
  restore: [],
  delete: [],
  conflicts: [],
  unrestorable: [],
  previewToken: "tok-1",
  ...over,
});

const file = { projectId: "p", path: "a.txt" };
const conflict = { ...file, reason: "changed since the agent wrote it" };
const stale = {
  status: 409,
  detail: "the files changed since the rewind was previewed",
};
const busy = {
  status: 409,
  detail: "A response is already in progress in this conversation",
};
const notFound = { status: 404, detail: "conversation not found" };

function run(opts: {
  previews: RewindPreview[];
  choices: RestoreChoice[];
  failFirstSend?: unknown;
  previewError?: unknown;
  proceedUnchecked?: boolean;
}) {
  const asked: { preview: RewindPreview; changed: boolean }[] = [];
  const sent: (string | undefined)[] = [];
  let fetches = 0;
  let sends = 0;
  let uncheckedAsked = 0;
  const result = sendWithRestore({
    fetchPreview: async () => {
      if (opts.previewError) throw opts.previewError;
      return opts.previews[Math.min(fetches++, opts.previews.length - 1)];
    },
    ask: async (p, changed) => {
      asked.push({ preview: p, changed });
      return opts.choices.shift() ?? "cancel";
    },
    send: async (restore) => {
      sent.push(restore?.preview_token);
      if (opts.failFirstSend && sends++ === 0 && restore)
        throw opts.failFirstSend;
      return "created";
    },
    isMissing: (err) => err === notFound,
    confirmUnchecked: async () => {
      uncheckedAsked++;
      return opts.proceedUnchecked ?? false;
    },
  });
  return { result, asked, sent, unchecked: () => uncheckedAsked };
}

describe("offering the file rewind before an edit or regenerate", () => {
  test("an empty dry run shows no dialog and sends untouched", async () => {
    const r = run({ previews: [preview()], choices: [] });
    expect(await r.result).toEqual({ sent: true, value: "created" });
    expect(r.asked).toHaveLength(0);
    expect(r.sent).toEqual([undefined]);
    expect(hasRewindFiles(preview())).toBe(false);
  });

  test("restoring sends the preview's token", async () => {
    const r = run({
      previews: [preview({ restore: [file] })],
      choices: ["restore"],
    });
    await r.result;
    expect(r.sent).toEqual(["tok-1"]);
  });

  test("continuing without restoring sends no token", async () => {
    const r = run({
      previews: [preview({ delete: [file] })],
      choices: ["skip"],
    });
    expect(await r.result).toEqual({ sent: true, value: "created" });
    expect(r.sent).toEqual([undefined]);
  });

  test("cancelling sends nothing", async () => {
    const r = run({
      previews: [preview({ restore: [file] })],
      choices: ["cancel"],
    });
    expect(await r.result).toEqual({ sent: false });
    expect(r.sent).toEqual([]);
  });

  test("conflicts alone are enough to ask", async () => {
    const r = run({
      previews: [preview({ conflicts: [conflict] })],
      choices: ["skip"],
    });
    await r.result;
    expect(r.asked[0].preview.conflicts).toEqual([conflict]);
  });

  test("the dialog lists files, marking conflicts and unrestorable ones", () => {
    const rows = rewindRows(
      preview({
        restore: [file],
        delete: [{ projectId: "p", path: "new.txt" }],
        conflicts: [conflict],
        unrestorable: [{ projectId: "p", path: "big.bin" }],
      }),
    );
    expect(rows.map((r) => [r.path, r.mark.label, r.mark.tone])).toEqual([
      ["a.txt", "Restore", "dim"],
      ["new.txt", "Delete", "dim"],
      ["a.txt", "Conflict", "warn"],
      ["big.bin", "No original", "warn"],
    ]);
    expect(rows[2].mark.note).toBe(conflict.reason);
  });

  test("files too large to restore are enough to ask", () => {
    expect(hasRewindFiles(preview({ unrestorable: [file] }))).toBe(true);
  });

  test("a 409 re-fetches the preview and asks again with the new list", async () => {
    const r = run({
      previews: [
        preview({ restore: [file] }),
        preview({
          restore: [file],
          conflicts: [conflict],
          previewToken: "tok-2",
        }),
      ],
      choices: ["restore", "restore"],
      failFirstSend: stale,
    });
    expect(await r.result).toEqual({ sent: true, value: "created" });
    expect(r.asked.map((a) => a.changed)).toEqual([false, true]);
    expect(r.sent).toEqual(["tok-1", "tok-2"]);
  });

  test("a busy-conversation 409 is rethrown, not re-previewed", async () => {
    const r = run({
      previews: [preview({ restore: [file] })],
      choices: ["restore"],
      failFirstSend: busy,
    });
    await expect(r.result).rejects.toBe(busy);
    expect(r.asked).toHaveLength(1);
  });

  test("only a stale-rewind 409 counts as stale", () => {
    expect(isStaleRewind(stale)).toBe(true);
    expect(isStaleRewind(busy)).toBe(false);
    expect(isStaleRewind(new Error("x"))).toBe(false);
  });

  test("a missing preview (404) sends untouched without asking", async () => {
    const r = run({ previews: [], choices: [], previewError: notFound });
    expect(await r.result).toEqual({ sent: true, value: "created" });
    expect(r.unchecked()).toBe(0);
    expect(r.sent).toEqual([undefined]);
  });

  test("any other preview failure asks, and going on sends untouched", async () => {
    const r = run({
      previews: [],
      choices: [],
      previewError: new Error("down"),
      proceedUnchecked: true,
    });
    expect(await r.result).toEqual({ sent: true, value: "created" });
    expect(r.unchecked()).toBe(1);
    expect(r.sent).toEqual([undefined]);
  });

  test("declining to go on without the check sends nothing", async () => {
    const r = run({
      previews: [],
      choices: [],
      previewError: new Error("down"),
    });
    expect(await r.result).toEqual({ sent: false });
    expect(r.sent).toEqual([]);
  });
});
