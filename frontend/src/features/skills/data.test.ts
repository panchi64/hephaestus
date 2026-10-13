import { afterEach, describe, expect, it, spyOn } from "bun:test";
import {
  setBuiltInSkillEnabled,
  toBuiltIn,
  type PlatformSkillOut,
} from "./data";

const dto: PlatformSkillOut = {
  name: "pdf-tools",
  description: "Work with PDFs",
  body: "# PDF\n\nSteps.",
  enabled: true,
  shadowed: false,
  files: ["references/forms.md"],
};

describe("toBuiltIn", () => {
  it("carries every wire field through", () => {
    expect(toBuiltIn(dto)).toEqual(dto);
  });

  it("keeps the shadowed and enabled flags independent", () => {
    const row = toBuiltIn({ ...dto, enabled: false, shadowed: true });
    expect(row.enabled).toBe(false);
    expect(row.shadowed).toBe(true);
  });
});

describe("setBuiltInSkillEnabled", () => {
  const fetchSpy = spyOn(globalThis, "fetch");

  afterEach(() => {
    fetchSpy.mockReset();
  });

  function respondWith(response: Response): void {
    fetchSpy.mockImplementation(
      Object.assign(() => Promise.resolve(response), {
        preconnect: () => {},
      }),
    );
  }

  function respondNoContent(): void {
    respondWith(new Response(null, { status: 204 }));
  }

  function lastCall(): { url: string; method: string | undefined } {
    const [input, init] = fetchSpy.mock.calls.at(-1) ?? [];
    return { url: String(input), method: init?.method };
  }

  it("POSTs to the enable endpoint", async () => {
    respondNoContent();
    await setBuiltInSkillEnabled("pdf-tools", true);
    const { url, method } = lastCall();
    expect(method).toBe("POST");
    expect(url.endsWith("/skills/platform/pdf-tools/enable")).toBe(true);
  });

  it("POSTs to the disable endpoint", async () => {
    respondNoContent();
    await setBuiltInSkillEnabled("pdf-tools", false);
    expect(lastCall().url.endsWith("/skills/platform/pdf-tools/disable")).toBe(
      true,
    );
  });

  it("rejects with the backend's error", async () => {
    respondWith(
      new Response(JSON.stringify({ detail: "Unknown skill" }), {
        status: 404,
      }),
    );
    await expect(setBuiltInSkillEnabled("nope", true)).rejects.toMatchObject({
      status: 404,
      detail: "Unknown skill",
    });
  });
});
