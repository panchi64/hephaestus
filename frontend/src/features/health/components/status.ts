import type { Status } from "~/ui";
import type { HealthStatus } from "../model";

/** `off` is not a fault, so it carries no hue (§5 rule 1). */
export const healthFlag: Record<HealthStatus, Status> = {
  nominal: "nominal",
  warn: "warn",
  alert: "alert",
  off: "idle",
};
