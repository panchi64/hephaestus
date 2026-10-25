import { describe, expect, test } from "bun:test";
import { MAX_MESSAGE_BYTES, parseRequest } from "./protocol";

describe("bridge requests", () => {
  test("a well-formed request passes with its params", () => {
    expect(
      parseRequest({ id: 1, method: "storage.get", params: { key: "best" } }),
    ).toEqual({ id: 1, method: "storage.get", params: { key: "best" } });
  });

  test("params default to an empty object", () => {
    expect(parseRequest({ id: 2, method: "window.close" })).toEqual({
      id: 2,
      method: "window.close",
      params: {},
    });
  });

  test.each([
    ["not an object", "storage.get"],
    [{ id: "1", method: "storage.get" }, "id"],
    [{ id: 1, method: "../../admin" }, "method"],
    [{ id: 1, method: "storage" }, "method"],
    [{ id: 1, method: "storage.get", params: [1] }, "params"],
  ])("refuses %p", (raw) => {
    expect(typeof parseRequest(raw)).toBe("string");
  });

  test("an oversized request is refused", () => {
    const big = "x".repeat(MAX_MESSAGE_BYTES);
    expect(
      parseRequest({ id: 1, method: "storage.set", params: { value: big } }),
    ).toBe("the request is too large");
  });
});
