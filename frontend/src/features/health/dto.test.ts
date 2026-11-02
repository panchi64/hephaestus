import { describe, expect, test } from "bun:test";
import { toRuntimeInfo, toServiceHealth, type ServiceHealthDTO } from "./dto";

const row = (over: Partial<ServiceHealthDTO> = {}): ServiceHealthDTO => ({
  key: "chat_model",
  kind: "capability",
  label: "CHAT MODEL",
  status: "nominal",
  detail: "1 endpoint",
  critical: true,
  remediation_href: null,
  remediation_label: null,
  checked_at: null,
  ...over,
});

describe("reading a service row", () => {
  test("the backend's verdict passes through, nulls become absent", () => {
    expect(toServiceHealth(row())).toEqual({
      key: "chat_model",
      kind: "capability",
      label: "CHAT MODEL",
      status: "nominal",
      detail: "1 endpoint",
      critical: true,
      remediation: undefined,
      checkedAt: undefined,
    });
  });

  test("a remediation path opens the settings section that fixes it", () => {
    const svc = toServiceHealth(
      row({
        status: "warn",
        remediation_href: "/models/embedding",
        remediation_label: "CONFIGURE",
      }),
    );
    expect(svc.remediation).toEqual({ label: "CONFIGURE", section: "models" });
  });

  test("a remediation path with no settings section is dropped", () => {
    const svc = toServiceHealth(
      row({ remediation_href: "/settings", remediation_label: "CONFIGURE" }),
    );
    expect(svc.remediation).toBeUndefined();
  });

  test("an endpoint keeps when it was last tested", () => {
    const svc = toServiceHealth(
      row({
        key: "endpoint:abc",
        kind: "endpoint",
        checked_at: "2026-11-02T10:00:00Z",
      }),
    );
    expect(svc.kind).toBe("endpoint");
    expect(svc.checkedAt).toBe("2026-11-02T10:00:00Z");
  });
});

describe("reading the runtime", () => {
  test("versions, roles and tool counts", () => {
    expect(
      toRuntimeInfo({
        version: "0.1.0",
        pydantic_ai: "2.64.0",
        pydantic_ai_harness: "0.64.0",
        python: "3.14.9",
        roles: [
          { role: "main", endpoint: "Local", model: "qwen", implicit: true },
          { role: "utility", endpoint: null, model: null, implicit: false },
        ],
        tools: { total: 40, enabled: 38 },
      }),
    ).toEqual({
      version: "0.1.0",
      pydanticAi: "2.64.0",
      pydanticAiHarness: "0.64.0",
      python: "3.14.9",
      roles: [
        { role: "main", endpoint: "Local", model: "qwen", implicit: true },
        {
          role: "utility",
          endpoint: undefined,
          model: undefined,
          implicit: false,
        },
      ],
      tools: { total: 40, enabled: 38 },
    });
  });
});
