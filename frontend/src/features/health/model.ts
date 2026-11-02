/** Health page data contracts. Each mirrors a backend shape (`GET /health/services`,
 *  `GET /runtime`); nothing here is measured client-side. */

/** `off`: not set up, switched off, or never tested — not a fault. */
export type HealthStatus = "nominal" | "warn" | "alert" | "off";

export interface ServiceHealth {
  /** Stable id: a capability key (`chat_model`, …) or `endpoint:<id>`. */
  key: string;
  kind: "capability" | "endpoint";
  label: string;
  status: HealthStatus;
  detail: string;
  /** The workspace cannot function without it — drives the overall status. */
  critical: boolean;
  /** The settings section that fixes it, when there is one. */
  remediation?: { label: string; section: string };
  /** When an endpoint was last connection-tested. Capabilities are live reads. */
  checkedAt?: string;
}

export interface RoleModel {
  role: string;
  /** Head of the role's endpoint chain; absent when the role is unbound. */
  endpoint?: string;
  model?: string;
  /** `main` with nothing bound, showing the backend's default. */
  implicit: boolean;
}

export interface RuntimeInfo {
  version: string;
  pydanticAi: string;
  pydanticAiHarness: string;
  python: string;
  roles: RoleModel[];
  tools: { total: number; enabled: number };
  /** Agent calls are traced in-process, feeding the Stats dialog's trace views. */
  tracing: boolean;
}
