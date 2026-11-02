import { createResource, type Resource } from "solid-js";
import { api } from "~/lib/api";
import {
  toRuntimeInfo,
  toServiceHealth,
  type RuntimeDTO,
  type ServiceHealthDTO,
} from "./dto";
import type { RuntimeInfo, ServiceHealth } from "./model";

async function fetchServices(): Promise<ServiceHealth[]> {
  const rows = await api.get<ServiceHealthDTO[]>("/health/services");
  return rows.map(toServiceHealth);
}

async function fetchRuntime(): Promise<RuntimeInfo> {
  return toRuntimeInfo(await api.get<RuntimeDTO>("/runtime"));
}

export interface Refetchable<T> {
  data: Resource<T>;
  refetch: () => Promise<unknown>;
}

export function useServiceHealth(): Refetchable<ServiceHealth[]> {
  const [data, { refetch }] = createResource(fetchServices);
  return { data, refetch: async () => refetch() };
}

export function useRuntime(): Refetchable<RuntimeInfo> {
  const [data, { refetch }] = createResource(fetchRuntime);
  return { data, refetch: async () => refetch() };
}
