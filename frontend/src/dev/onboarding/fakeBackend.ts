import { API_BASE } from "~/lib/config";
import type { EndpointViewDTO, RoleViewDTO } from "~/lib/api/models-types";
import {
  CLOUD_MODELS,
  CLOUD_WINDOW,
  PROVIDERS,
  type Scenario,
} from "./scenarios";

type Json = unknown;

interface FakeState {
  completed: boolean;
  endpoints: EndpointViewDTO[];
  roles: Record<string, RoleViewDTO>;
  nextId: number;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function json(status: number, body: Json): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const fail = (status: number, detail: string) => json(status, { detail });

/** An in-browser stand-in for the routes the walkthrough reaches, so the dev bench runs
 *  the real components with no backend, no session and nothing of the operator's. It
 *  answers only requests under `API_BASE`; anything it doesn't model is a 404, which is
 *  what an unknown route would be. Returns the restore function. */
export function installFakeBackend(scenario: Scenario): () => void {
  const state: FakeState = {
    completed: false,
    endpoints: structuredClone(scenario.endpoints),
    roles: structuredClone(scenario.roles),
    nextId: 100,
  };

  const serverFor = (baseUrl: string) =>
    scenario.servers.find(
      (s) => s.base_url.replace(/\/+$/, "") === baseUrl.replace(/\/+$/, ""),
    );

  const modelsOf = (ep: EndpointViewDTO): string[] => {
    const local = serverFor(ep.base_url);
    if (local) return local.models.map((m) => m.id);
    return CLOUD_MODELS[ep.provider] ?? (ep.model ? [ep.model] : []);
  };

  const windowOf = (
    ep: EndpointViewDTO,
    model: string | null,
  ): number | null => {
    if (ep.context_window) return ep.context_window;
    const local = serverFor(ep.base_url);
    if (local)
      return local.models.find((m) => m.id === model)?.context_window ?? null;
    return ep.provider in CLOUD_MODELS ? CLOUD_WINDOW : null;
  };

  /** Mirrors the backend: a role reports its head's window, and an unbound `main`
   *  reports the first working endpoint's first model as an implicit default. */
  const rolesView = (): Record<string, RoleViewDTO> => {
    const view: Record<string, RoleViewDTO> = {};
    for (const [role, binding] of Object.entries(state.roles)) {
      const head = state.endpoints.find(
        (e) => e.id === binding.endpoint_ids[0],
      );
      view[role] = {
        ...binding,
        context_window: head ? windowOf(head, binding.model) : null,
      };
    }
    if (!view.main) {
      const usable = state.endpoints.find((e) => e.enabled);
      const model = usable ? (modelsOf(usable)[0] ?? null) : null;
      if (usable && model)
        view.main = {
          endpoint_ids: [usable.id],
          model,
          context_window: windowOf(usable, model),
          implicit: true,
        };
    }
    return view;
  };

  /** The connection test: a local server answers if the scan saw it; a cloud provider
   *  answers if it has a key; any URL containing "fail" never answers. */
  const probe = (
    ep: EndpointViewDTO,
  ): Pick<
    EndpointViewDTO,
    "last_status" | "last_error_category" | "last_error_detail"
  > => {
    if (ep.base_url.includes("fail") || ep.id === "ep-bad")
      return {
        last_status: "error",
        last_error_category: "unreachable",
        last_error_detail: `Unreachable — nothing answered at ${ep.base_url}.`,
      };
    const provider = PROVIDERS.find((p) => p.id === ep.provider);
    if (provider?.requires_key && !ep.has_api_key)
      return {
        last_status: "error",
        last_error_category: "auth",
        last_error_detail: "The provider rejected the request: no API key.",
      };
    return {
      last_status: "ok",
      last_error_category: "ok",
      last_error_detail: `Reachable — ${modelsOf(ep).length} models listed.`,
    };
  };

  const fromBody = (body: Record<string, unknown>, base: EndpointViewDTO) => {
    const next = { ...base };
    for (const [key, value] of Object.entries(body)) {
      if (key === "api_key") next.has_api_key = value !== "";
      else if (key in next) (next as Record<string, unknown>)[key] = value;
    }
    return next;
  };

  const route = async (
    method: string,
    path: string,
    body: Record<string, unknown>,
  ): Promise<Response> => {
    if (path === "/auth/status")
      return json(200, {
        initialized: true,
        unlocked: true,
        auth_enabled: false,
        db_missing: false,
      });
    if (path === "/auth/lock" || path === "/auth/logout") return json(200, {});

    if (path === "/onboarding")
      return json(200, { completed: state.completed });
    if (path === "/onboarding/local-endpoints") {
      await sleep(scenario.scanMs);
      return json(200, scenario.servers);
    }
    if (path === "/onboarding/reopen") {
      state.completed = false;
      return json(200, { completed: false });
    }
    if (path === "/onboarding/complete") {
      if (!state.endpoints.some((e) => e.enabled && e.last_status === "ok"))
        return fail(409, "No endpoint has passed its connection test.");
      const main = state.roles.main;
      if (!main?.endpoint_ids.length)
        return fail(409, "No chat model is picked.");
      if (rolesView().main?.context_window == null)
        return fail(
          409,
          "The chat model's endpoint reports no context window.",
        );
      state.completed = true;
      return json(200, { completed: true });
    }

    if (path === "/models/providers") return json(200, PROVIDERS);
    if (path === "/models/roles") return json(200, rolesView());
    const role = path.match(/^\/models\/roles\/(\w+)$/);
    if (role && method === "PUT") {
      const ids = (body.endpoint_ids as string[] | undefined) ?? [];
      if (ids.length === 0) delete state.roles[role[1]];
      else
        state.roles[role[1]] = {
          endpoint_ids: ids,
          model: (body.model as string | null | undefined) ?? null,
          context_window: null,
          implicit: false,
        };
      return json(204, null);
    }

    if (path === "/models/endpoints" && method === "GET")
      return json(200, state.endpoints);
    if (path === "/models/endpoints" && method === "POST") {
      const provider = PROVIDERS.find((p) => p.id === body.provider);
      if (provider?.requires_key && !body.api_key)
        return fail(422, `${provider.display_name} needs an API key.`);
      const created = fromBody(body, {
        id: `ep-${state.nextId++}`,
        name: "",
        base_url: "",
        provider: "openai-compatible",
        model: null,
        has_api_key: false,
        context_window: null,
        max_concurrent_runs: null,
        native_tools: true,
        vision: false,
        thinking: false,
        enabled: true,
        last_status: null,
        last_error_category: null,
        last_error_detail: null,
        last_checked_at: null,
      });
      state.endpoints.push(created);
      return json(201, created);
    }
    const ep = path.match(/^\/models\/endpoints\/([\w-]+)(\/\w+)?$/);
    if (ep) {
      const index = state.endpoints.findIndex((e) => e.id === ep[1]);
      if (index < 0) return fail(404, "model endpoint not found");
      const target = state.endpoints[index];
      if (ep[2] === "/models")
        return json(200, { models: modelsOf(target), supported: true });
      if (ep[2] === "/test") {
        await sleep(500);
        const verdict = probe(target);
        const checked = new Date().toISOString();
        state.endpoints[index] = {
          ...target,
          ...verdict,
          last_checked_at: checked,
        };
        return json(200, {
          status: verdict.last_status,
          error_category: verdict.last_error_category,
          error_detail: verdict.last_error_detail,
          checked_at: checked,
        });
      }
      if (method === "PATCH") {
        state.endpoints[index] = fromBody(body, target);
        return json(200, state.endpoints[index]);
      }
      if (method === "DELETE") {
        state.endpoints.splice(index, 1);
        return json(204, null);
      }
      return json(200, target);
    }

    if (path === "/models/embedding/reindex")
      return json(200, {
        state: "idle",
        memories: 0,
        messages: 0,
        detail: null,
        completed_at: null,
      });
    if (path === "/overview") return json(200, { capabilities: [] });
    if (path === "/search/providers") return json(200, []);
    if (path === "/vm") return json(200, { state: "ready", detail: null });
    return fail(404, `not modelled by the dev bench: ${method} ${path}`);
  };

  const real = window.fetch;
  const fake = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    if (!url.startsWith(API_BASE)) return real.call(window, input, init);
    const { pathname } = new URL(url);
    const method = (init?.method ?? "GET").toUpperCase();
    const raw = typeof init?.body === "string" ? init.body : "";
    const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    return route(method, pathname, body);
  };
  window.fetch = Object.assign(fake, { preconnect: real.preconnect });
  return () => {
    window.fetch = real;
  };
}
