import type { Status } from "~/ui";
import { overallStatus } from "~/features/dashboard/activity";
import type { HealthStatus, ServiceHealth } from "../model";

/** `off` is not a fault, so it carries no hue (§5 rule 1). */
export const healthFlag: Record<HealthStatus, Status> = {
  nominal: "nominal",
  warn: "warn",
  alert: "alert",
  off: "idle",
};

export type OverallHealth = ReturnType<typeof overallStatus>;

/** Capabilities roll up as on the dashboard. An endpoint row is only its last stored
 *  test, possibly stale or on a spare endpoint, so a failed one is at most a warning. */
export function overallHealth(rows: readonly ServiceHealth[]): OverallHealth {
  const caps = overallStatus(rows.filter((r) => r.kind === "capability"));
  const failedEndpoint = rows.some(
    (r) => r.kind === "endpoint" && r.status === "alert",
  );
  return caps === "nominal" && failedEndpoint ? "warn" : caps;
}
