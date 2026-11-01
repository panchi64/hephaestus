import { For, Show, type JSX } from "solid-js";
import {
  Button,
  Panel,
  Row,
  Stack,
  StatusDot,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { isApiError } from "~/lib/api";
import { setRoleBinding, useEndpoints, useRoles } from "../data";
import { BINDABLE_ROLES } from "../model";
import { healthStatus } from "./EndpointHealthFlag";
import type { ModelEndpoint } from "~/lib/stores/models";

/* The second half of a role binding: `utility` and `embedding` each resolve
   against an ordered fallback chain (first = primary), and the control below
   captures that order explicitly — membership *and* position — so it doesn't ride
   on endpoint creation order. WHICH model each job uses is picked on its card
   above; this only decides where the request goes when the primary is down.
   `main` has no chain (it is single-endpoint), so it isn't listed here. */
export function FallbackChainsSection(): JSX.Element {
  const endpoints = useEndpoints();
  const roles = useRoles();

  const chainFor = (role: string): string[] =>
    roles()?.[role]?.endpointIds ?? [];
  const modelFor = (role: string): string | null =>
    roles()?.[role]?.model ?? null;
  const endpointById = (id: string): ModelEndpoint | undefined =>
    (endpoints() ?? []).find((e) => e.id === id);
  const endpointName = (id: string): string => endpointById(id)?.name ?? id;
  // A chain member's health for the fallback-chain dots: a disabled endpoint
  // reads as dead (the chain auto-switches past it), else its last probe verdict.
  const chainHealth = (id: string): "nominal" | "alert" | "idle" => {
    const ep = endpointById(id);
    if (!ep || !ep.enabled) return "alert";
    return healthStatus(ep.lastStatus);
  };
  const unboundFor = (role: string): ModelEndpoint[] => {
    const bound = new Set(chainFor(role));
    return (endpoints() ?? []).filter((e) => !bound.has(e.id));
  };

  // Re-bind preserves the role's pinned model — reordering where a request goes
  // must never silently change which model answers it. A backend rejection (e.g.
  // a non-embeddings model) surfaces its detail to the operator verbatim.
  const applyChain = async (role: string, next: string[]) => {
    try {
      const reindexStarted = await setRoleBinding(role, next, modelFor(role));
      if (reindexStarted)
        toast.info("Re-embedding memories and chats for the new model…");
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : `Unable to update the ${role} role.`,
      );
    }
  };
  const addToRole = (role: string, id: string) =>
    applyChain(role, [...chainFor(role), id]);
  const removeFromRole = (role: string, id: string) =>
    applyChain(
      role,
      chainFor(role).filter((x) => x !== id),
    );
  const moveInRole = (role: string, index: number, dir: -1 | 1) => {
    const chain = [...chainFor(role)];
    const j = index + dir;
    if (j < 0 || j >= chain.length) return;
    [chain[index], chain[j]] = [chain[j], chain[index]];
    return applyChain(role, chain);
  };

  return (
    <Panel label="Fallback chains">
      <Stack gap={4}>
        <Text variant="caption" tone="dim">
          Where a request goes when the primary endpoint is down. The background
          and search &amp; memory jobs each bind an ordered chain (first =
          primary) and auto-switch past dead or disabled endpoints. The chat
          model is single-endpoint and has no chain.
        </Text>
        <Show
          when={(endpoints() ?? []).length}
          fallback={
            <Text variant="caption" tone="dim">
              Add an endpoint to bind a chain.
            </Text>
          }
        >
          <For each={BINDABLE_ROLES}>
            {(role) => (
              <Stack gap={2}>
                <Text variant="label" tone="bright">
                  {role.toUpperCase()}
                </Text>
                <Show
                  when={chainFor(role).length}
                  fallback={
                    <Text variant="caption" tone="dim">
                      No endpoints bound — add one below.
                    </Text>
                  }
                >
                  <Stack gap={0}>
                    <For each={chainFor(role)}>
                      {(id, i) => (
                        <Row
                          align="center"
                          justify="between"
                          gap={2}
                          class="py-1.5"
                        >
                          <Row gap={2} align="center" class="min-w-0">
                            <Text variant="micro" tone="dim">
                              {i() + 1}
                            </Text>
                            <StatusDot status={chainHealth(id)} />
                            <Text
                              variant="label"
                              tone="default"
                              class="truncate"
                            >
                              {endpointName(id)}
                            </Text>
                            <Show when={i() === 0}>
                              <StatusFlag status="nominal">Primary</StatusFlag>
                            </Show>
                          </Row>
                          <span class="flex shrink-0 items-center gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              leading="chevron-up"
                              aria-label="Move earlier in the chain"
                              disabled={i() === 0}
                              onClick={() => void moveInRole(role, i(), -1)}
                            />
                            <Button
                              variant="ghost"
                              size="sm"
                              leading="chevron-down"
                              aria-label="Move later in the chain"
                              disabled={i() === chainFor(role).length - 1}
                              onClick={() => void moveInRole(role, i(), 1)}
                            />
                            <Button
                              variant="ghost"
                              size="sm"
                              leading="close"
                              aria-label="Remove from the chain"
                              onClick={() => void removeFromRole(role, id)}
                            />
                          </span>
                        </Row>
                      )}
                    </For>
                  </Stack>
                </Show>
                <Show when={unboundFor(role).length}>
                  <div class="flex flex-wrap gap-2">
                    <For each={unboundFor(role)}>
                      {(ep) => (
                        <Button
                          variant="ghost"
                          size="sm"
                          leading="plus"
                          onClick={() => void addToRole(role, ep.id)}
                        >
                          {ep.name}
                        </Button>
                      )}
                    </For>
                  </div>
                </Show>
              </Stack>
            )}
          </For>
        </Show>
      </Stack>
    </Panel>
  );
}
