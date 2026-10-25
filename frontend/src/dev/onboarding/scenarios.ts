import type {
  EndpointViewDTO,
  ProviderViewDTO,
  RoleViewDTO,
} from "~/lib/api/models-types";

/** What the fake `/onboarding/local-endpoints` answers, in its wire shape. */
export interface FakeLocalServer {
  label: string;
  base_url: string;
  models: {
    id: string;
    context_window: number | null;
    parallel_slots: number | null;
    loaded: boolean | null;
    kind: string | null;
  }[];
}

export interface Scenario {
  id: string;
  label: string;
  /** One line on what this scenario exercises, shown in the bench toolbar. */
  note: string;
  servers: FakeLocalServer[];
  endpoints: EndpointViewDTO[];
  roles: Record<string, RoleViewDTO>;
  /** How long the localhost scan takes, so the throbber can be seen. */
  scanMs: number;
}

export const PROVIDERS: ProviderViewDTO[] = [
  {
    id: "openai-compatible",
    display_name: "OpenAI-compatible",
    requires_key: false,
    default_base_url: null,
    key_hint: null,
    docs_url: null,
    native_tools: true,
    vision: false,
  },
  {
    id: "anthropic",
    display_name: "Anthropic",
    requires_key: true,
    default_base_url: "https://api.anthropic.com",
    key_hint: "sk-ant-…",
    docs_url: null,
    native_tools: true,
    vision: true,
  },
  {
    id: "google",
    display_name: "Google",
    requires_key: true,
    default_base_url: "https://generativelanguage.googleapis.com",
    key_hint: "AIza…",
    docs_url: null,
    native_tools: true,
    vision: true,
  },
];

/** Models a cloud provider lists once its key is accepted. */
export const CLOUD_MODELS: Record<string, string[]> = {
  anthropic: ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"],
  google: ["gemini-3-pro", "gemini-3-flash"],
};

/** The window the fake reports for a cloud model. */
export const CLOUD_WINDOW = 200_000;

const LM_STUDIO: FakeLocalServer = {
  label: "LM Studio",
  base_url: "http://127.0.0.1:1234/v1",
  models: [
    {
      id: "qwen3-coder-30b-a3b",
      context_window: 65_536,
      parallel_slots: 4,
      loaded: true,
      kind: "llm",
    },
    {
      id: "gpt-oss-120b",
      context_window: 131_072,
      parallel_slots: null,
      loaded: false,
      kind: "llm",
    },
    {
      id: "text-embedding-nomic-embed-text-v1.5",
      context_window: 2_048,
      parallel_slots: null,
      loaded: false,
      kind: "embeddings",
    },
  ],
};

const LLAMA_SERVER_NO_WINDOW: FakeLocalServer = {
  label: "llama-server",
  base_url: "http://127.0.0.1:8080/v1",
  models: [
    {
      id: "glm-4.6-q4",
      context_window: null,
      parallel_slots: null,
      loaded: null,
      kind: null,
    },
  ],
};

function endpoint(over: Partial<EndpointViewDTO>): EndpointViewDTO {
  return {
    id: "ep-1",
    name: "LM Studio",
    provider: "openai-compatible",
    base_url: LM_STUDIO.base_url,
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
    ...over,
  };
}

export const SCENARIOS: Scenario[] = [
  {
    id: "local",
    label: "Local server found",
    note: "LM Studio answers with two chat models and an embedding model.",
    servers: [LM_STUDIO],
    endpoints: [],
    roles: {},
    scanMs: 1200,
  },
  {
    id: "nothing",
    label: "Nothing local",
    note: "The scan finds nothing; a provider has to be entered by hand.",
    servers: [],
    endpoints: [],
    roles: {},
    scanMs: 1200,
  },
  {
    id: "no-window",
    label: "No context window",
    note: "llama-server reports no window, so the chat step asks for one.",
    servers: [LLAMA_SERVER_NO_WINDOW],
    endpoints: [],
    roles: {},
    scanMs: 600,
  },
  {
    id: "failing",
    label: "Failing endpoint",
    note: "An endpoint is configured but its last test failed.",
    servers: [],
    endpoints: [
      endpoint({
        id: "ep-bad",
        name: "Workstation",
        base_url: "http://10.0.0.9:1234/v1",
        last_status: "error",
        last_error_category: "unreachable",
        last_error_detail: "Unreachable — nothing answered at 10.0.0.9:1234.",
        last_checked_at: "2026-10-24T09:00:00Z",
      }),
    ],
    roles: {},
    scanMs: 600,
  },
  {
    id: "resume",
    label: "Required steps done",
    note: "A reopen from Settings: it resumes at Roles.",
    servers: [LM_STUDIO],
    endpoints: [
      endpoint({ last_status: "ok", last_checked_at: "2026-10-24T09:00:00Z" }),
    ],
    roles: {
      main: {
        endpoint_ids: ["ep-1"],
        model: "qwen3-coder-30b-a3b",
        context_window: 65_536,
        implicit: false,
      },
    },
    scanMs: 400,
  },
];

export function scenarioById(id: string | null): Scenario {
  return SCENARIOS.find((s) => s.id === id) ?? SCENARIOS[0];
}
