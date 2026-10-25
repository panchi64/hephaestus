import { isApiError } from "~/lib/api";
import type { ModelEndpoint } from "~/lib/stores/models";
import { toast } from "~/ui";
import { saveEndpointForm } from "../data";
import type { EndpointFormValues } from "./EndpointForm";

/** Saves an endpoint form and says how it went; the saved id, or null on failure. */
export async function saveEndpointWithToast(
  target: ModelEndpoint | null,
  values: EndpointFormValues,
): Promise<string | null> {
  try {
    const id = await saveEndpointForm(target, values);
    toast.success(target ? "Endpoint updated" : "Endpoint added");
    return id;
  } catch (e) {
    // A 422 (e.g. a key-requiring provider without a key) carries a plain-language
    // detail from the backend, rendered verbatim.
    toast.error(isApiError(e) ? e.detail : "Unable to save the endpoint.");
    return null;
  }
}
