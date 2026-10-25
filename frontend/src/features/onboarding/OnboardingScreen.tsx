import {
  createEffect,
  createResource,
  createSignal,
  Match,
  Show,
  Switch,
  type JSX,
} from "solid-js";
import { settled } from "~/lib/resource";
import { isApiError } from "~/lib/api";
import { ConstructionReveal, toast } from "~/ui";
import { refreshEndpoints, refreshRoles } from "~/lib/stores/models";
import { useEndpoints, useRoles } from "~/features/settings/data";
import { completeOnboarding, fetchLocalServers } from "./data";
import {
  chatReady,
  endpointReady,
  resumeStep,
  STEPS,
  stepIndex,
  workingEndpoints,
  type LocalServer,
  type StepId,
} from "./model";
import { BriefingSlot } from "./components/briefingSlot";
import { ChatModelStep } from "./components/ChatModelStep";
import { TalosBriefing } from "./components/TalosBriefing";
import { EndpointStep } from "./components/EndpointStep";
import { PreparationRail, type RailEntry } from "./components/PreparationRail";
import { ReadyStep } from "./components/ReadyStep";
import { RolesStep } from "./components/RolesStep";
import { WorkspaceStep } from "./components/WorkspaceStep";

const RAIL_WIDTH = 260;

/** The first-run walkthrough: Talos's preparation checklist on the left, one step at a
 *  time in the column. Opens where the workspace stands, so a reload or a reopen from
 *  Settings resumes at the first unmet requirement. */
export function OnboardingScreen(): JSX.Element {
  const endpoints = useEndpoints();
  const roles = useRoles();
  // A failed scan is an empty one: the operator can still point at a server by hand.
  const [scan] = createResource(() => fetchLocalServers().catch(() => []));
  const servers = (): LocalServer[] => settled(scan) ?? [];
  const [step, setStep] = createSignal<StepId | null>(null);
  const [visited, setVisited] = createSignal<Set<StepId>>(new Set());
  const [briefing, setBriefing] = createSignal("");
  /** A refused finish, waiting for fresh data to say which step to reopen. */
  const [resyncing, setResyncing] = createSignal(false);

  createEffect(() => {
    const eps = settled(endpoints);
    const bindings = settled(roles);
    if (step() === null && eps && bindings)
      setStep(resumeStep(eps, bindings.main));
  });
  createEffect(() => {
    const s = step();
    if (s) setVisited((v) => new Set(v).add(s));
  });

  const okEndpoints = () => workingEndpoints(settled(endpoints) ?? []);
  const isDone = (id: StepId): boolean => {
    switch (id) {
      case "endpoint":
        return endpointReady(settled(endpoints) ?? []);
      case "chat":
        return chatReady(settled(roles)?.main, settled(endpoints) ?? []);
      case "ready":
        return false;
      default:
        return visited().has(id) && stepIndex(id) < stepIndex(step() ?? id);
    }
  };
  const reachable = (id: StepId): boolean =>
    STEPS.slice(0, stepIndex(id)).every((s) => !s.required || isDone(s.id));

  const boundCount = (): number =>
    (["utility", "decision", "embedding"] as const).filter(
      (r) => (settled(roles)?.[r]?.endpointIds.length ?? 0) > 0,
    ).length;

  const entries = (): Record<StepId, RailEntry> => ({
    endpoint: {
      done: isDone("endpoint"),
      readout: isDone("endpoint") ? `${okEndpoints().length} OK` : "REQUIRED",
    },
    chat: {
      done: isDone("chat"),
      readout: isDone("chat") ? "SET" : "REQUIRED",
    },
    roles: {
      done: isDone("roles"),
      readout: boundCount() > 0 ? `${boundCount()} SET` : "DEFAULT",
    },
    workspace: {
      done: isDone("workspace"),
      readout: isDone("workspace") ? "DONE" : "OPTIONAL",
    },
    ready: { done: false, readout: "" },
  });

  const go = (id: StepId): void => {
    if (reachable(id)) setStep(id);
  };
  const next = (): void => {
    const s = step();
    const following = s ? STEPS[stepIndex(s) + 1] : undefined;
    if (following) go(following.id);
  };
  const back = (): void => {
    const s = step();
    const previous = s ? STEPS[stepIndex(s) - 1] : undefined;
    if (previous) setStep(previous.id);
  };

  const enter = async (): Promise<void> => {
    try {
      await completeOnboarding();
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : "Unable to finish the walkthrough.",
      );
      // Refresh first so both resources are loading by the time the resync effect reads them.
      refreshEndpoints();
      refreshRoles();
      setResyncing(true);
    }
  };
  createEffect(() => {
    if (!resyncing() || endpoints.loading || roles.loading) return;
    setResyncing(false);
    setStep(resumeStep(settled(endpoints) ?? [], settled(roles)?.main));
  });

  return (
    <div class="flex h-screen overflow-hidden text-text">
      <aside
        class="shrink-0 overflow-hidden"
        style={{ width: `${RAIL_WIDTH}px` }}
      >
        <PreparationRail
          current={step() ?? "endpoint"}
          scanning={scan.loading}
          found={servers().length}
          entries={entries()}
          reachable={reachable}
          onGo={go}
        />
      </aside>
      <main class="flex min-w-0 flex-1 justify-center overflow-y-auto px-6 py-12">
        <ConstructionReveal
          when={step() !== null}
          origin="top-left"
          class="my-auto h-fit w-full max-w-xl"
          contentClass="flex flex-col gap-4 p-6"
        >
          <TalosBriefing text={briefing()} />
          <BriefingSlot.Provider value={setBriefing}>
            <Show when={step()} keyed>
              {(current) => (
                <Switch>
                  <Match when={current === "endpoint"}>
                    <EndpointStep
                      servers={settled(scan)}
                      scanning={scan.loading}
                      onContinue={next}
                    />
                  </Match>
                  <Match when={current === "chat"}>
                    <ChatModelStep
                      servers={servers()}
                      onBack={back}
                      onContinue={next}
                    />
                  </Match>
                  <Match when={current === "roles"}>
                    <RolesStep
                      servers={servers()}
                      onBack={back}
                      onContinue={next}
                    />
                  </Match>
                  <Match when={current === "workspace"}>
                    <WorkspaceStep onBack={back} onContinue={next} />
                  </Match>
                  <Match when={current === "ready"}>
                    <ReadyStep onEdit={go} onBack={back} onEnter={enter} />
                  </Match>
                </Switch>
              )}
            </Show>
          </BriefingSlot.Provider>
        </ConstructionReveal>
      </main>
    </div>
  );
}
