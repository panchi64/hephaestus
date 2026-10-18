import { Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { PageHeader, Stack, Tabs, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import { useTabParam } from "~/lib/useTabParam";
import {
  decodeModelValue,
  effectiveSelection,
  effectiveValue,
  encodeModelValue,
  modelPickerGroups,
  refreshEndpoints,
  refreshModels,
  type ModelEndpoint,
} from "~/lib/stores/models";
import {
  setRoleBinding,
  setSelectedModel,
  useEndpoints,
  useRoles,
} from "../data";
import { EmbeddingRoleControls } from "../components/EmbeddingRoleControls";
import { EndpointsSection } from "../components/EndpointsSection";
import { FallbackChainsSection } from "../components/FallbackChainsSection";
import { ModelRoleCard } from "../components/ModelRoleCard";

/** The one home for "which model does what".
 *
 *  Three jobs, named the way an operator thinks about them rather than by the
 *  backend's role ids: the model that answers you (`main`), the cheap one that
 *  does background work (`utility`), and the one that powers recall
 *  (`embedding`). Each card offers a single combined picker over the SAME
 *  discovery groups the top-bar picker reads, so the two surfaces cannot
 *  disagree about what's selected, and each writes through the store's existing
 *  role actions — no role call is issued here.
 *
 *  Every model is reached through an **endpoint** — an OpenAI-compatible URL the
 *  operator points at, whether it is a server on this machine or a lab's API.
 *  Hephaestus no longer downloads weights, installs engines, or supervises a local
 *  server, so the hardware band and the serve lifecycle that used to sit under
 *  these cards are gone; what a model runs on is decided where it runs.
 *
 *  **Two tabs, not a disclosure.** Endpoint plumbing (which endpoints exist) and
 *  fallback ordering (where a request goes when the primary is down) used to sit
 *  behind an ADVANCED chevron, which meant adding an endpoint — the first thing
 *  anyone does here — required finding a collapsed control first. ENDPOINTS is
 *  now a named half of this pane: still out of the way of the choice most people
 *  came to make, but visible from the moment the pane opens.
 *
 *  The tab rides in the URL (`?models=endpoints`), so a link can point at the
 *  endpoint list directly and the back button undoes a switch. */
const TABS = ["assignments", "endpoints"] as const;

export function ModelsScreen(): JSX.Element {
  const endpoints = useEndpoints();
  const roles = useRoles();
  const [tab, setTab] = useTabParam("models", TABS, "assignments");

  const endpointById = (id: string | undefined): ModelEndpoint | undefined =>
    id ? (settled(endpoints) ?? []).find((e) => e.id === id) : undefined;

  /** Re-ask the endpoints what they serve, whenever one of the pickers below is
   *  opened. This page is where an operator lands *after* starting a local engine
   *  or adding a key elsewhere, so the list it shows is the one most likely to be
   *  out of date — and nothing about the stored endpoint rows changes when a
   *  provider gains a model, so only asking again can find it. */
  const rediscover = () => {
    refreshEndpoints();
    refreshModels();
  };

  /* ── CHAT ──────────────────────────────────────────────────────────────────
     Read and written through exactly the accessors the top-bar picker uses, so
     the two controls are the same control wearing different chrome. */
  const chatEndpoint = () => endpointById(effectiveSelection()?.endpointId);
  const pickChat = async (value: string) => {
    const sel = decodeModelValue(value);
    if (!sel) return;
    try {
      await setSelectedModel(sel);
      toast.success(`Chat model set to ${sel.model}.`);
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to set the chat model.");
    }
  };

  /* ── The bound roles ───────────────────────────────────────────────────────
     A role's pick as one picker value: the pinned model if there is one, else
     the head endpoint's own default — which is what the backend resolves to, so
     the picker shows the model that actually runs rather than an
     "endpoint default" abstraction. Picking always pins explicitly. */
  const roleEndpoint = (role: string): ModelEndpoint | undefined =>
    endpointById(roles()?.[role]?.endpointIds?.[0]);
  /** The role's chain with `head` promoted to primary and every other member kept
   *  behind it. Picking a *model* must not silently change where a request goes when
   *  the primary is down — the mirror of the invariant `FallbackChainsSection` holds
   *  (reordering preserves the pinned model). Writing `[head]` outright would drop the
   *  fallbacks configured on the ENDPOINTS tab with nothing on screen saying so. */
  const chainHeadedBy = (role: string, head: string): string[] => [
    head,
    ...(roles()?.[role]?.endpointIds ?? []).filter((id) => id !== head),
  ];
  const roleValue = (role: string): string => {
    const ep = roleEndpoint(role);
    if (!ep) return "";
    const model = roles()?.[role]?.model ?? ep.model;
    return model ? encodeModelValue({ endpointId: ep.id, model }) : "";
  };

  /* An unbound `utility` is not a broken state: the backend degrades background
     work onto the chat model. So "same as chat model" is spelled as the empty
     binding it already is, rather than as a fourth thing to configure. */
  const SAME_AS_CHAT = "";
  const backgroundGroups = () => [
    {
      label: "Default",
      options: [{ value: SAME_AS_CHAT, label: "Same as chat model" }],
    },
    ...modelPickerGroups(),
  ];
  const pickBackground = async (value: string) => {
    const sel = value === SAME_AS_CHAT ? null : decodeModelValue(value);
    if (value !== SAME_AS_CHAT && !sel) return;
    try {
      await setRoleBinding(
        "utility",
        sel ? chainHeadedBy("utility", sel.endpointId) : [],
        sel?.model ?? null,
      );
      toast.success(
        sel
          ? `Background model set to ${sel.model}.`
          : "Background work now uses the chat model.",
      );
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : "Unable to set the background model.",
      );
    }
  };

  /** Binds `role` to the picked endpoint and model, naming it `name` in the toasts. */
  const bindPicked = async (role: string, name: string, value: string) => {
    const sel = decodeModelValue(value);
    if (!sel) return;
    try {
      const reindexStarted = await setRoleBinding(
        role,
        chainHeadedBy(role, sel.endpointId),
        sel.model,
      );
      if (reindexStarted)
        toast.info("Re-embedding memories and chats for the new model…");
      else toast.success(`${name} set to ${sel.model}.`);
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : `Unable to set the ${name.toLowerCase()}.`,
      );
    }
  };
  const pickEmbedding = (value: string) =>
    bindPicked("embedding", "Search & memory model", value);
  const pickDecision = (value: string) =>
    bindPicked("decision", "Decision model", value);

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Models"
        subtitle="Which model does what. Set it here once — every surface follows."
        assetId="HEPH-MDL-01.0"
      />

      <Tabs
        items={[
          { value: "assignments", label: "Assignments" },
          { value: "endpoints", label: "Endpoints" },
        ]}
        value={tab()}
        onChange={(v) => setTab(v as (typeof TABS)[number])}
      />

      <Show when={tab() === "assignments"}>
        <Stack gap={6}>
          <ModelRoleCard
            label="Chat model"
            description="Answers you in chat, research, and tasks."
            groups={modelPickerGroups()}
            value={effectiveValue()}
            onChange={(v) => void pickChat(v)}
            onOpen={rediscover}
            placeholder="No model"
            endpoint={chatEndpoint()}
            implicit={settled(roles)?.main?.implicit ?? false}
          />

          <ModelRoleCard
            label="Background model"
            description="Titles, summaries, verification. A cheaper model is usually the right call."
            groups={backgroundGroups()}
            value={roleValue("utility")}
            onChange={(v) => void pickBackground(v)}
            onOpen={rediscover}
            placeholder="Same as chat model"
            endpoint={roleEndpoint("utility")}
          />

          <ModelRoleCard
            label="Search & memory model"
            description="Powers recall across memories and chats. Changing this re-indexes everything."
            groups={modelPickerGroups()}
            value={roleValue("embedding")}
            onChange={(v) => void pickEmbedding(v)}
            onOpen={rediscover}
            placeholder="Not set — recall is keyword-only"
            endpoint={roleEndpoint("embedding")}
          >
            <EmbeddingRoleControls
              bound={roleEndpoint("embedding") !== undefined}
            />
          </ModelRoleCard>

          <ModelRoleCard
            label="Decision model"
            description="Rules on Auto's actions without asking you. Needs a decision endpoint: llama-server's /v1/systemone, or OpenRouter's decision models."
            groups={modelPickerGroups()}
            value={roleValue("decision")}
            onChange={(v) => void pickDecision(v)}
            onOpen={rediscover}
            placeholder="Not set — Auto asks you instead"
            endpoint={roleEndpoint("decision")}
          />
        </Stack>
      </Show>

      <Show when={tab() === "endpoints"}>
        <Stack gap={6}>
          <EndpointsSection />
          <FallbackChainsSection />
        </Stack>
      </Show>
    </Stack>
  );
}
