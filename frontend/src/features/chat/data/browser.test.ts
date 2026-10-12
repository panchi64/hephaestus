import { describe, expect, test } from "bun:test";
import { parseBrowserEvent } from "./browser";

const line = (value: unknown): string => `data: ${JSON.stringify(value)}`;

describe("parseBrowserEvent", () => {
  test("decodes a frame", () => {
    expect(
      parseBrowserEvent(
        line({
          type: "frame",
          data: "QUJD",
          width: 1280,
          height: 720,
          url: "https://example.com/",
          title: "Example",
        }),
      ),
    ).toEqual({
      type: "frame",
      data: "QUJD",
      width: 1280,
      height: 720,
      url: "https://example.com/",
      title: "Example",
    });
  });

  test("defaults a missing url or title to empty", () => {
    expect(
      parseBrowserEvent(
        line({ type: "frame", data: "x", width: 10, height: 10 }),
      ),
    ).toEqual({
      type: "frame",
      data: "x",
      width: 10,
      height: 10,
      url: "",
      title: "",
    });
  });

  test("decodes closed", () => {
    expect(parseBrowserEvent(line({ type: "closed" }))).toEqual({
      type: "closed",
    });
  });

  test("reads the data line out of a multi-line frame", () => {
    expect(
      parseBrowserEvent(`event: message\n${line({ type: "closed" })}`),
    ).toEqual({ type: "closed" });
  });

  test("ignores keepalives, unknown events and malformed frames", () => {
    expect(parseBrowserEvent(": ping")).toBeNull();
    expect(parseBrowserEvent("data: {nope")).toBeNull();
    expect(parseBrowserEvent("data: 42")).toBeNull();
    expect(parseBrowserEvent(line({ type: "other" }))).toBeNull();
    expect(
      parseBrowserEvent(
        line({ type: "frame", data: "x", width: 0, height: 5 }),
      ),
    ).toBeNull();
    expect(
      parseBrowserEvent(line({ type: "frame", width: 5, height: 5 })),
    ).toBeNull();
  });
});
