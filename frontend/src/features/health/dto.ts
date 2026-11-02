import type {
  HealthStatus,
  RoleModel,
  RuntimeInfo,
  ServiceHealth,
} from "./model";

/** One row of `GET /health/services` (`ServiceHealth` in `routes/health.py`). */
export interface ServiceHealthDTO {
  key: string;
  kind: "capability" | "endpoint";
  label: string;
  status: HealthStatus;
  detail: string;
  critical: boolean;
  remediation_href: string | null;
  remediation_label: string | null;
  checked_at: string | null;
}

interface RoleModelDTO {
  role: string;
  endpoint: string | null;
  model: string | null;
  implicit: boolean;
}

/** `GET /runtime` (`Runtime` in `routes/runtime.py`). */
export interface RuntimeDTO {
  version: string;
  pydantic_ai: string;
  pydantic_ai_harness: string;
  python: string;
  roles: RoleModelDTO[];
  tools: { total: number; enabled: number };
}

/** Backend remediation paths → the settings section that fixes them. The paths predate
 *  the settings dialog; one with no section here would only open the 404 page, so it
 *  is dropped. */
const REMEDIATION_SECTIONS: Record<string, string> = {
  "/models": "models",
  "/models/embedding": "models",
};

export function toServiceHealth(dto: ServiceHealthDTO): ServiceHealth {
  const section = dto.remediation_href
    ? REMEDIATION_SECTIONS[dto.remediation_href]
    : undefined;
  return {
    key: dto.key,
    kind: dto.kind,
    label: dto.label,
    status: dto.status,
    detail: dto.detail,
    critical: dto.critical,
    remediation:
      section && dto.remediation_label
        ? { label: dto.remediation_label, section }
        : undefined,
    checkedAt: dto.checked_at ?? undefined,
  };
}

function toRoleModel(dto: RoleModelDTO): RoleModel {
  return {
    role: dto.role,
    endpoint: dto.endpoint ?? undefined,
    model: dto.model ?? undefined,
    implicit: dto.implicit,
  };
}

export function toRuntimeInfo(dto: RuntimeDTO): RuntimeInfo {
  return {
    version: dto.version,
    pydanticAi: dto.pydantic_ai,
    pydanticAiHarness: dto.pydantic_ai_harness,
    python: dto.python,
    roles: dto.roles.map(toRoleModel),
    tools: { total: dto.tools.total, enabled: dto.tools.enabled },
  };
}
