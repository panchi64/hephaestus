import { describe, expect, test } from "bun:test";
import { envelopeLabel, noticeLabel, openTaskCount } from "./envelopeLabels";
import type { ContentPart } from "./model";

const untrusted = (source?: string): ContentPart => ({
  kind: "untrusted",
  text: "body",
  source,
});

describe("envelopeLabel", () => {
  test("tool output from outside is External, named by its source", () => {
    expect(envelopeLabel(untrusted("https://a.test/x"), "tool")).toEqual({
      label: "External",
      detail: "https://a.test/x",
    });
  });

  test("a project instruction file is named by its path", () => {
    expect(
      envelopeLabel(
        { kind: "context_file", text: "rules", path: "AGENTS.md" },
        "tool",
      ),
    ).toEqual({ label: "Project instructions", detail: "AGENTS.md" });
  });

  test("the routine's previous run is named as such wherever it sits", () => {
    const part = untrusted("the routine's previous run");
    expect(envelopeLabel(part, "message")).toEqual({ label: "Previous run" });
  });

  test("untrusted text in the operator's turn is attachment text", () => {
    expect(envelopeLabel(untrusted("report.pdf"), "message")).toEqual({
      label: "Attachment text",
      detail: "report.pdf",
    });
  });
});

describe("noticeLabel", () => {
  test("open tasks are counted from the bullet lines", () => {
    expect(openTaskCount("Open:\n- one\n- two\n  - nested")).toBe(3);
    expect(noticeLabel("open-tasks", "- a\n- b\n- c")).toEqual({
      label: "Reminder",
      detail: "3 tasks open",
    });
    expect(noticeLabel("open-tasks", "- a").detail).toBe("1 task open");
  });

  test("any other kind is a generic notice", () => {
    expect(noticeLabel("something-else", "x")).toEqual({
      label: "Notice",
      detail: "something-else",
    });
  });
});
