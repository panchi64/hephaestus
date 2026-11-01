import { describe, expect, test } from "bun:test";
import { allowableHost, boundaryReadout } from "./commandBoundary";
import { commandBoundary } from "./data/hostCommands";

describe("the boundary line", () => {
  test("a VM command reports its reach and its fence", () => {
    expect(
      boundaryReadout({ reach: "network", fenced: true, network: true }),
    ).toEqual({
      reach: "network",
      ranWithNetwork: false,
      fence: "fenced",
      unfencedReason: undefined,
    });
  });

  test("an offline declaration that ran with network says so", () => {
    expect(
      boundaryReadout({ reach: "offline", fenced: true, network: true })
        ?.ranWithNetwork,
    ).toBe(true);
  });

  test("a host command without a reach shows the verdict alone, with its reason", () => {
    expect(
      boundaryReadout({ fenced: false, unfencedReason: "no fence here" }),
    ).toEqual({
      reach: undefined,
      ranWithNetwork: false,
      fence: "unfenced",
      unfencedReason: "no fence here",
    });
  });

  test("a result carrying neither says nothing", () => {
    expect(boundaryReadout({})).toBeNull();
  });
});

describe("the denied host", () => {
  test("a VM command's refusal can be allowed", () => {
    expect(
      allowableHost({ reach: "network", deniedHost: "pypi.example" }),
    ).toBe("pypi.example");
    expect(allowableHost({ reach: "offline", deniedHost: "a.example" })).toBe(
      "a.example",
    );
  });

  test("a host-reach refusal gets none", () => {
    expect(allowableHost({ reach: "host", deniedHost: "a.example" })).toBe(
      undefined,
    );
  });

  test("nothing refused, nothing to allow", () => {
    expect(allowableHost({ reach: "network" })).toBe(undefined);
  });

  test("it is read off both tools' results by the same keys", () => {
    const vm = commandBoundary({
      ok: false,
      reach: "network",
      fenced: true,
      fence_note: "blocked",
      denied_host: "files.example",
    });
    expect(vm?.deniedHost).toBe("files.example");
    const host = commandBoundary({
      ok: true,
      fenced: false,
      unfenced_reason: "the host fence is not applied: off",
      stdout: "",
    });
    expect(host?.fenced).toBe(false);
    expect(host?.deniedHost).toBeUndefined();
  });
});
