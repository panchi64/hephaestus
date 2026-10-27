import {
  createSignal,
  For,
  Show,
  Suspense,
  onCleanup,
  type JSX,
} from "solid-js";
import { createStore } from "solid-js/store";
import {
  Button,
  Drawer,
  EmptyState,
  Icon,
  InfoHint,
  InstrumentBand,
  LoadingText,
  Marquee,
  Menu,
  type MenuItem,
  NotConnectedOverlay,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
  type Status,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { timestamp } from "~/lib/format";
import { useServiceStatuses, useOverallHealth } from "../data";
import type { HealthStatus, ServiceStatus } from "../model";

const healthTone: Record<HealthStatus, string> = {
  nominal: "bg-nominal",
  warn: "bg-warn",
  alert: "bg-alert",
  timeout: "bg-warn",
  partial: "bg-info",
};

const healthFlagStatus: Record<HealthStatus, Status> = {
  nominal: "nominal",
  warn: "warn",
  alert: "alert",
  timeout: "warn",
  partial: "info",
};

/** Maps HealthStatus to the nearest TextTone for InstrumentBand cells. */
const healthTextTone: Record<
  HealthStatus,
  "bright" | "warn" | "alert" | "info" | "dim"
> = {
  nominal: "bright",
  warn: "warn",
  alert: "alert",
  timeout: "warn",
  partial: "info",
};

/** Service-specific recovery actions for Phase 1 (toasts as feedback, Phase 2 wires to backend). */
function getServiceActions(
  svc: ServiceStatus,
  onAction: (label: string) => void,
) {
  const base: MenuItem[] = [
    {
      label: "View logs",
      icon: "note" as const,
      onSelect: () => onAction(`Viewing logs for ${svc.name}`),
    },
  ];

  if (svc.status === "alert" || svc.status === "timeout") {
    base.unshift({
      label: "Retry connection",
      icon: "refresh" as const,
      onSelect: () => onAction(`Retrying connection for ${svc.name}`),
    });
  }

  if (svc.id === "svc-embed") {
    base.unshift({
      label: "Reindex",
      icon: "database" as const,
      onSelect: () => onAction(`Reindex queued for ${svc.name}`),
    });
  }

  if (svc.status === "partial") {
    base.unshift({
      label: "Retry partial",
      icon: "refresh" as const,
      onSelect: () => onAction(`Retry queued for ${svc.name}`),
    });
  }

  return base;
}

/** Health checks run on a fixed interval; each tick = one probe. */
const CHECK_INTERVAL = "30s";
const HISTORY_HINT = `Last 10 health checks (newest on the right). One tick = one probe, every ${CHECK_INTERVAL} — spans ~5 min.`;

function HistoryBar(props: {
  history: HealthStatus[];
  showHint?: boolean;
}): JSX.Element {
  return (
    <div class="flex items-center gap-1" title={HISTORY_HINT}>
      {/* Hint sits LEFT of the bars so the bars themselves stay flush against
          the column's right edge and line up across every row. */}
      <Show when={props.showHint}>
        <InfoHint label={HISTORY_HINT} size={11} />
      </Show>
      <div class="flex items-center gap-0.5">
        <For each={props.history}>
          {(h) => (
            <span
              class={`inline-block h-3 w-1.5 ${healthTone[h]}`}
              style={{ opacity: h === "nominal" ? "0.7" : "1" }}
            />
          )}
        </For>
      </div>
    </div>
  );
}

/** Drawer showing detail and recovery actions for a single service. */
function ServiceDrawer(props: {
  svc: ServiceStatus | null;
  onClose: () => void;
}): JSX.Element {
  function handleAction(label: string) {
    props.onClose();
    toast.info(`${label} — available in Phase 2`, { duration: 3500 });
  }

  return (
    <Drawer
      open={props.svc !== null}
      onClose={props.onClose}
      title={props.svc?.name ?? ""}
    >
      <Show when={props.svc}>
        {(svc) => (
          <Stack gap={4}>
            <Row gap={2} align="center">
              <StatusFlag status={healthFlagStatus[svc().status]}>
                {svc().status.toUpperCase()}
              </StatusFlag>
              <Text variant="body" tone="dim">
                {svc().detail}
              </Text>
            </Row>

            <Show when={svc().degradationNote}>
              <Panel label="Degradation note" state="alert">
                <Text
                  variant="body"
                  tone={svc().status === "alert" ? "alert" : "warn"}
                >
                  {svc().degradationNote}
                </Text>
              </Panel>
            </Show>

            <Stack gap={2}>
              <Text variant="label" tone="dim">
                Recovery actions
              </Text>
              <For each={getServiceActions(svc(), handleAction)}>
                {(action) => (
                  <Button
                    variant="ghost"
                    leading={action.icon}
                    onClick={action.onSelect}
                    class="w-full justify-start"
                  >
                    {action.label}
                  </Button>
                )}
              </For>
              <Show
                when={
                  svc().status === "nominal" &&
                  getServiceActions(svc(), handleAction).length <= 1
                }
              >
                <Text variant="caption" tone="dim">
                  No recovery actions needed — service is healthy.
                </Text>
              </Show>
            </Stack>

            <Stack gap={1}>
              <Row gap={1} align="center">
                <Text variant="label" tone="dim">
                  HISTORY · EVERY {CHECK_INTERVAL}
                </Text>
                <InfoHint label={HISTORY_HINT} size={11} />
              </Row>
              <HistoryBar history={svc().history} />
            </Stack>

            <Show when={svc().latencyMs > 0}>
              <Row gap={2} align="baseline">
                <Text variant="micro" tone="dim">
                  LATENCY: {svc().latencyMs}MS
                </Text>
                <Text variant="micro" tone="dim">
                  · {svc().baselineMs}MS baseline
                </Text>
                <InfoHint
                  label="Baseline = typical latency for this service under normal load. Live latency well above baseline indicates degradation."
                  size={11}
                />
              </Row>
            </Show>
          </Stack>
        )}
      </Show>
    </Drawer>
  );
}

export function HealthScreen(): JSX.Element {
  const overallResource = useOverallHealth();
  const servicesResource = useServiceStatuses();
  const [services, setServices] = createStore<ServiceStatus[]>([]);
  const [seeded, setSeeded] = createSignal(false);
  const [refreshing, setRefreshing] = createSignal(false);
  const [lastRefresh, setLastRefresh] = createSignal(new Date().toISOString());
  const [drawerSvc, setDrawerSvc] = createSignal<ServiceStatus | null>(null);
  const timers: ReturnType<typeof setTimeout>[] = [];

  onCleanup(() => timers.forEach(clearTimeout));

  const resolveServices = () => {
    const data = servicesResource();
    if (data && !seeded()) {
      setSeeded(true);
      setServices(data.map((s) => ({ ...s, history: [...s.history] })));
    }
    return data;
  };

  function refresh() {
    if (refreshing()) return;

    const prevDegradedIds = new Set(
      services.filter((s) => s.degradationNote).map((s) => s.id),
    );

    setRefreshing(true);
    timers.push(
      setTimeout(() => {
        setRefreshing(false);
        const now = new Date();
        setLastRefresh(now.toISOString());

        // Determine if any degradations resolved since last check.
        const resolvedCount = services.filter(
          (s) => prevDegradedIds.has(s.id) && !s.degradationNote,
        ).length;

        const timeStr = now.toLocaleTimeString("en-US", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });

        if (resolvedCount > 0) {
          toast.success(
            `${resolvedCount} issue${resolvedCount > 1 ? "s" : ""} resolved — checked at ${timeStr}`,
          );
        } else {
          toast.success(`Status checked — ${timeStr}`);
        }
      }, 1100),
    );
  }

  const overall = () => overallResource();
  const alertCount = () => services.filter((s) => s.status === "alert").length;
  const warnCount = () =>
    services.filter(
      (s) =>
        s.status === "warn" || s.status === "timeout" || s.status === "partial",
    ).length;

  return (
    /* Health is the one surface still on fixtures, and it has to keep saying so.
       That used to be a route-level flag (`connected` in the nav model) that the
       shell read to lay a NOT CONNECTED overlay over the page; Health is a
       section of the settings dialog now and has no route to carry the flag, so
       the overlay is rendered here, inline, over the section it actually
       describes. `relative` is what it positions against.

       It goes when the backend measures per-service latency, a baseline, and a
       status history — none of which it does today. */
    <Stack gap={6} class="relative">
      <NotConnectedOverlay />
      <SettingsPageHeader
        section="health"
        status={
          <Show when={overall()}>
            {(o) => (
              <StatusFlag
                status={healthFlagStatus[o().status]}
                dot={o().status !== "nominal"}
              >
                {o().status.toUpperCase()}
              </StatusFlag>
            )}
          </Show>
        }
        actions={
          <Button
            variant="ghost"
            leading="refresh"
            onClick={refresh}
            disabled={refreshing()}
          >
            {refreshing() ? "Checking…" : "Refresh"}
          </Button>
        }
      />

      <Suspense fallback={<LoadingText label="Checking services" />}>
        <Show when={overall()}>
          {(o) => (
            <InstrumentBand
              items={[
                {
                  label: "Overall",
                  value: o().status.toUpperCase(),
                  tone: healthTextTone[o().status],
                },
                {
                  label: "Up",
                  value: `${o().servicesUp} / ${o().servicesTotal}`,
                },
                {
                  label: "Alerts",
                  value: String(alertCount()),
                  tone: alertCount() > 0 ? "alert" : "dim",
                },
                {
                  label: "Warnings",
                  value: String(warnCount()),
                  tone: warnCount() > 0 ? "warn" : "dim",
                },
                { label: "Last check", value: timestamp(lastRefresh()) },
                { label: "Uptime", value: "99.1%" },
              ]}
            />
          )}
        </Show>
      </Suspense>

      <Suspense fallback={<LoadingText label="Loading services" />}>
        <Show when={resolveServices()}>
          <Show
            when={services.length}
            fallback={<EmptyState icon="activity" message="No services" />}
          >
            <Panel label="Service grid" flush class="@container">
              {/* One shared grid drives every row so the columns — crucially the
                  uptime HISTORY bars — line up vertically no matter how much
                  detail a given service carries. Each row is a `subgrid` that
                  spans the parent tracks, so its cells inherit (and contribute
                  to) the shared column sizing. Below ~40rem the row restacks
                  into two lines (identity + status up top, metrics below) so the
                  dense right-hand content stops overflowing at narrow widths. */}
              <div class="grid grid-cols-[auto_1fr_auto_auto] @max-[40rem]:grid-cols-[1fr_auto]">
                <For each={services}>
                  {(svc) => (
                    <div
                      role="button"
                      tabindex={0}
                      onClick={() => setDrawerSvc(svc)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setDrawerSvc(svc);
                        }
                      }}
                      class="col-span-full grid grid-cols-subgrid cursor-pointer items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-left transition-colors hover:bg-raised"
                    >
                      {/* IDENTITY — anchors the top line when stacked. */}
                      <span class="col-start-1 row-start-1 flex min-w-0 items-center gap-2">
                        <Icon name="activity" class="shrink-0 text-dim" />
                        <Text variant="label" class="truncate">
                          {svc.name}
                        </Text>
                      </span>

                      {/* METRICS + NOTE — a long degradation note marquees in the
                          flexible slot rather than widening the row. Drops to the
                          second line (under identity) when stacked. */}
                      <span class="col-start-2 row-start-1 flex min-w-0 items-center gap-3 @max-[40rem]:col-start-1 @max-[40rem]:row-start-2">
                        <Show
                          when={svc.degradationNote}
                          fallback={<span class="flex-1" />}
                        >
                          <Marquee class="min-w-0 flex-1">
                            <Text
                              variant="micro"
                              tone={svc.status === "alert" ? "alert" : "warn"}
                            >
                              {svc.degradationNote}
                            </Text>
                          </Marquee>
                        </Show>
                        <Text variant="micro" tone="dim" class="shrink-0">
                          {svc.detail}
                        </Text>
                        <Show
                          when={
                            svc.status !== "alert" && svc.status !== "timeout"
                          }
                          fallback={
                            <Text
                              variant="micro"
                              tone={svc.status === "timeout" ? "warn" : "alert"}
                              class="shrink-0"
                            >
                              {svc.status === "timeout" ? "Timeout" : "Offline"}
                            </Text>
                          }
                        >
                          <span
                            class="shrink-0"
                            title={`Baseline ${svc.baselineMs}MS`}
                          >
                            <Text
                              variant="micro"
                              tone={
                                svc.latencyMs > svc.baselineMs * 2
                                  ? "warn"
                                  : "dim"
                              }
                            >
                              {svc.latencyMs}
                              <span class="text-dim">/{svc.baselineMs}MS</span>
                            </Text>
                          </span>
                        </Show>
                      </span>

                      {/* HISTORY — its own column keeps every bar aligned across
                          rows. When stacked it shares the right column with the
                          (wider) status cluster, so `justify-end` keeps the bars
                          pinned to the same right edge, directly under the badge. */}
                      <span class="col-start-3 row-start-1 flex shrink-0 items-center justify-end @max-[40rem]:col-start-2 @max-[40rem]:row-start-2">
                        <HistoryBar history={svc.history} showHint />
                      </span>

                      {/* ACTIONS + STATUS — top-right; stays on the top line when
                          stacked, paired with the service name. STATUS sits last
                          so the badge always anchors to the right edge and every
                          row's badge lines up, whether or not ACTIONS is present.
                          When stacked it spans the whole top line (right-aligned)
                          so its width no longer dictates the right column — that
                          keeps the column as narrow as the history bars, so the
                          description below sits right up against the info icon. */}
                      <span class="col-start-4 row-start-1 flex shrink-0 items-center justify-end gap-3 @max-[40rem]:col-span-full">
                        <Show
                          when={
                            svc.status === "alert" ||
                            svc.status === "warn" ||
                            svc.status === "timeout" ||
                            svc.status === "partial"
                          }
                        >
                          <span onClick={(e) => e.stopPropagation()}>
                            <Menu
                              trigger={
                                <Button variant="ghost" size="sm">
                                  Actions
                                </Button>
                              }
                              items={getServiceActions(svc, (label) => {
                                toast.info(`${label} — available in Phase 2`, {
                                  duration: 3500,
                                });
                              })}
                              align="right"
                            />
                          </span>
                        </Show>
                        <StatusFlag status={healthFlagStatus[svc.status]}>
                          {svc.status.toUpperCase()}
                        </StatusFlag>
                      </span>
                    </div>
                  )}
                </For>
              </div>
            </Panel>
          </Show>
        </Show>
      </Suspense>

      <Show when={services.filter((s) => s.degradationNote).length > 0}>
        <Panel label="Degradation notes" state="alert">
          <Stack gap={3}>
            <For each={services.filter((s) => s.degradationNote)}>
              {(svc) => (
                <Row gap={3} align="center">
                  <StatusFlag status={healthFlagStatus[svc.status]}>
                    {svc.name}
                  </StatusFlag>
                  <Text
                    variant="body"
                    tone={svc.status === "alert" ? "alert" : "warn"}
                  >
                    {svc.degradationNote}
                  </Text>
                </Row>
              )}
            </For>
          </Stack>
        </Panel>
      </Show>

      <ServiceDrawer svc={drawerSvc()} onClose={() => setDrawerSvc(null)} />
    </Stack>
  );
}
