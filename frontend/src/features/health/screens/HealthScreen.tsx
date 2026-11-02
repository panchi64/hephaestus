import { createSignal, Show, type JSX } from "solid-js";
import {
  Button,
  InstrumentBand,
  Resource,
  Stack,
  StatusFlag,
  type TextTone,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { timestamp } from "~/lib/format";
import { settled } from "~/lib/resource";
import { overallStatus } from "~/features/dashboard/activity";
import { useRuntime, useServiceHealth } from "../data";
import type { ServiceHealth } from "../model";
import { RuntimeCard } from "../components/RuntimeCard";
import { ServiceGrid } from "../components/ServiceGrid";
import { ServiceDrawer } from "../components/ServiceDrawer";
import { healthFlag } from "../components/status";

const overallTone: Record<ReturnType<typeof overallStatus>, TextTone> = {
  nominal: "bright",
  warn: "warn",
  alert: "alert",
};

export function HealthScreen(): JSX.Element {
  const services = useServiceHealth();
  const runtime = useRuntime();
  const [checkedAt, setCheckedAt] = createSignal(new Date().toISOString());
  const [refreshing, setRefreshing] = createSignal(false);
  const [drawerSvc, setDrawerSvc] = createSignal<ServiceHealth | null>(null);

  async function refresh() {
    if (refreshing()) return;
    setRefreshing(true);
    try {
      await Promise.all([services.refetch(), runtime.refetch()]);
      setCheckedAt(new Date().toISOString());
    } finally {
      setRefreshing(false);
    }
  }

  const rows = () => settled(services.data) ?? [];
  const overall = () => overallStatus(rows());
  const count = (s: ServiceHealth["status"]) =>
    rows().filter((r) => r.status === s).length;

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="health"
        status={
          <Show when={rows().length}>
            <StatusFlag
              status={healthFlag[overall()]}
              dot={overall() !== "nominal"}
            >
              {overall().toUpperCase()}
            </StatusFlag>
          </Show>
        }
        actions={
          <Button
            variant="ghost"
            leading="refresh"
            onClick={() => void refresh()}
            disabled={refreshing()}
          >
            {refreshing() ? "Checking…" : "Refresh"}
          </Button>
        }
      />

      <Resource data={runtime.data} onRetry={() => void runtime.refetch()}>
        {(info) => <RuntimeCard runtime={info()} />}
      </Resource>

      <Resource
        data={services.data}
        onRetry={() => void services.refetch()}
        loadingLabel="Checking services"
        isEmpty={(s) => s.length === 0}
        emptyMessage="No services"
      >
        {(all) => (
          <Stack gap={6}>
            <InstrumentBand
              items={[
                {
                  label: "Overall",
                  value: overall().toUpperCase(),
                  tone: overallTone[overall()],
                },
                {
                  label: "Nominal",
                  value: `${count("nominal")} / ${all().length}`,
                },
                {
                  label: "Alerts",
                  value: String(count("alert")),
                  tone: count("alert") > 0 ? "alert" : "dim",
                },
                {
                  label: "Warnings",
                  value: String(count("warn")),
                  tone: count("warn") > 0 ? "warn" : "dim",
                },
                { label: "Last check", value: timestamp(checkedAt()) },
              ]}
            />
            <ServiceGrid
              label="Capabilities"
              services={all().filter((s) => s.kind === "capability")}
              onSelect={setDrawerSvc}
            />
            <Show when={all().some((s) => s.kind === "endpoint")}>
              <ServiceGrid
                label="Model endpoints"
                services={all().filter((s) => s.kind === "endpoint")}
                onSelect={setDrawerSvc}
              />
            </Show>
          </Stack>
        )}
      </Resource>

      <ServiceDrawer svc={drawerSvc()} onClose={() => setDrawerSvc(null)} />
    </Stack>
  );
}
