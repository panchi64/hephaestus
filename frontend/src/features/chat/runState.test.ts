import { describe, expect, test } from "bun:test";
import { runStateSpec } from "./runState";

describe("a run parked on the operator", () => {
  test("reads as an approval when no kind is known", () => {
    const spec = runStateSpec("awaiting_input");
    expect(spec.readout).toBe("NEEDS APPROVAL");
    expect(spec.status).toBe("warn");
  });

  test("a question reads in the info tone", () => {
    const spec = runStateSpec("awaiting_input", "question");
    expect(spec.readout).toBe("QUESTION");
    expect(spec.status).toBe("info");
    expect(spec.live).toBe(true);
  });

  test("a conflict reads in the alert tone", () => {
    const spec = runStateSpec("awaiting_input", "alert");
    expect(spec.readout).toBe("NEEDS DECISION");
    expect(spec.status).toBe("alert");
  });

  test("the kind changes nothing for any other state", () => {
    expect(runStateSpec("running", "question")).toEqual(
      runStateSpec("running"),
    );
  });
});
