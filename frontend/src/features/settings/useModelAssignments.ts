import { settled } from "~/lib/resource";
import { isApiError } from "~/lib/api";
import { toast } from "~/ui";
import {
  decodeModelValue,
  effectiveSelection,
  encodeModelValue,
  refreshEndpoints,
  refreshModels,
  type ModelEndpoint,
  type ModelSelection,
} from "~/lib/stores/models";
import {
  setRoleBinding,
  setSelectedModel,
  useEndpoints,
  useRoles,
} from "./data";

/** The four jobs a model can be given, by the backend's role id. */
export type AssignableRole = "main" | "utility" | "embedding" | "decision";

/** An unbound `utility` is not a broken state: the backend degrades background work onto
 *  the chat model, so "same as chat model" is spelled as the empty binding it already is. */
export const SAME_AS_CHAT = "";

/** Reads and writes for "which model does what", shared by Settings › Models and the
 *  first-run walkthrough. Every write goes through the store's role actions, and the chat
 *  pick through the same action the composer's picker uses, so no surface can disagree. */
export function useModelAssignments() {
  const endpoints = useEndpoints();
  const roles = useRoles();

  const endpointById = (id: string | undefined): ModelEndpoint | undefined =>
    id ? (settled(endpoints) ?? []).find((e) => e.id === id) : undefined;

  /** Re-ask the endpoints what they serve whenever a picker opens: a model loaded since
   *  the page did is otherwise invisible, since no endpoint row changes when it appears. */
  const rediscover = (): void => {
    refreshEndpoints();
    refreshModels();
  };

  const chatEndpoint = (): ModelEndpoint | undefined =>
    endpointById(effectiveSelection()?.endpointId);

  const pickChat = async (sel: ModelSelection): Promise<boolean> => {
    try {
      await setSelectedModel(sel);
      return true;
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to set the chat model.");
      return false;
    }
  };

  const roleEndpoint = (role: AssignableRole): ModelEndpoint | undefined =>
    endpointById(settled(roles)?.[role]?.endpointIds?.[0]);

  /** The role's chain with `head` promoted to primary and every other member kept behind
   *  it: picking a model must not silently drop the fallbacks configured on the ENDPOINTS
   *  tab (the invariant `FallbackChainsSection` holds when reordering). */
  const chainHeadedBy = (role: AssignableRole, head: string): string[] => [
    head,
    ...(settled(roles)?.[role]?.endpointIds ?? []).filter((id) => id !== head),
  ];

  /** A role's pick as one picker value: the pinned model, else the head endpoint's own
   *  default — what the backend resolves to. Picking always pins explicitly. */
  const roleValue = (role: AssignableRole): string => {
    const ep = roleEndpoint(role);
    if (!ep) return "";
    const model = settled(roles)?.[role]?.model ?? ep.model;
    return model ? encodeModelValue({ endpointId: ep.id, model }) : "";
  };

  const pickBackground = async (value: string): Promise<void> => {
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
  const bindPicked = async (
    role: AssignableRole,
    name: string,
    value: string,
  ): Promise<void> => {
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

  /** Unbinds `role`, back to its default: background work on the chat model, keyword
   *  recall, Auto asking the operator. */
  const clearRole = async (
    role: AssignableRole,
    name: string,
  ): Promise<void> => {
    try {
      await setRoleBinding(role, [], null);
      toast.success(`${name} is back to its default.`);
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : `Unable to reset the ${name.toLowerCase()}.`,
      );
    }
  };

  return {
    endpoints,
    roles,
    rediscover,
    chatEndpoint,
    pickChat,
    roleEndpoint,
    roleValue,
    pickBackground,
    bindPicked,
    clearRole,
  };
}
