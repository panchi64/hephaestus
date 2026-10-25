import { describe, expect, test } from "bun:test";
import type {
  ModelEndpoint,
  ModelGroup,
  RoleBinding,
} from "~/lib/stores/models";
import {
  chatChoices,
  chatReady,
  endpointReady,
  formatTokens,
  preferredChoice,
  resumeStep,
  scannedModel,
  stepCounter,
  unclaimedServers,
  type LocalModel,
  type LocalServer,
} from "./model";

/**
 * The walkthrough's pure decisions: where it resumes, what counts as a working chat
 * setup, and which scanned models to offer.
 *
 * These gate a first-run flow that cannot be retried cheaply. A disabled endpoint that
 * still reads as ready, a base URL that differs only by a trailing slash, or an
 * embedding model preselected as the chat model each strand the operator on a step that
 * looks complete but fails at `POST /onboarding/complete`.
 */

function endpoint(over: Partial<ModelEndpoint> = {}): ModelEndpoint {
  return {
    id: "ep1",
    name: "Local",
    provider: "openai-compatible",
    baseUrl: "http://localhost:1234/v1",
    model: null,
    hasApiKey: false,
    contextWindow: null,
    maxConcurrentRuns: null,
    nativeTools: true,
    vision: false,
    thinking: false,
    enabled: true,
    lastStatus: "ok",
    lastErrorCategory: null,
    lastErrorDetail: null,
    lastCheckedAt: null,
    ...over,
  };
}

function binding(over: Partial<RoleBinding> = {}): RoleBinding {
  return {
    endpointIds: ["ep1"],
    model: "m1",
    contextWindow: 65536,
    implicit: false,
    ...over,
  };
}

function group(endpointId: string, models: string[]): ModelGroup {
  return {
    endpointId,
    endpointName: endpointId,
    choices: models.map((model) => ({ model, endpointId })),
  };
}

function localModel(id: string, over: Partial<LocalModel> = {}): LocalModel {
  return {
    id,
    contextWindow: null,
    parallelSlots: null,
    loaded: null,
    kind: null,
    ...over,
  };
}

function server(baseUrl: string, models: LocalModel[] = []): LocalServer {
  return { label: baseUrl, baseUrl, models };
}

describe("stepCounter", () => {
  test("renders the one-based position zero-padded over the total", () => {
    expect(stepCounter("endpoint")).toBe("01 / 05");
    expect(stepCounter("roles")).toBe("03 / 05");
    expect(stepCounter("ready")).toBe("05 / 05");
  });
});

describe("endpointReady", () => {
  test("an enabled endpoint that tested ok is ready", () => {
    expect(endpointReady([endpoint()])).toBe(true);
  });

  test("a disabled endpoint does not count even when its last probe was ok", () => {
    expect(
      endpointReady([endpoint({ enabled: false, lastStatus: "ok" })]),
    ).toBe(false);
  });

  test("an untested endpoint does not count", () => {
    expect(endpointReady([endpoint({ lastStatus: null })])).toBe(false);
  });

  test("an endpoint whose probe errored does not count", () => {
    expect(endpointReady([endpoint({ lastStatus: "error" })])).toBe(false);
  });

  test("one good endpoint among bad ones is enough", () => {
    expect(
      endpointReady([
        endpoint({ id: "a", lastStatus: "error" }),
        endpoint({ id: "b" }),
      ]),
    ).toBe(true);
  });

  test("no endpoints is not ready", () => {
    expect(endpointReady([])).toBe(false);
  });
});

describe("chatReady", () => {
  test("a missing main binding is not ready", () => {
    expect(chatReady(undefined)).toBe(false);
  });

  test("an implicit binding is not ready, whatever its window", () => {
    expect(chatReady(binding({ implicit: true }))).toBe(false);
  });

  test("an explicit binding without a context window is not ready", () => {
    expect(chatReady(binding({ contextWindow: null }))).toBe(false);
  });

  test("an explicit binding with a window is ready", () => {
    expect(chatReady(binding())).toBe(true);
  });
});

describe("resumeStep", () => {
  test("no working endpoint resumes at the endpoint step", () => {
    expect(resumeStep([endpoint({ lastStatus: null })], binding())).toBe(
      "endpoint",
    );
  });

  test("a working endpoint without a ready chat model resumes at chat", () => {
    expect(resumeStep([endpoint()], undefined)).toBe("chat");
  });

  test("both requirements met resumes at the first optional step", () => {
    expect(resumeStep([endpoint()], binding())).toBe("roles");
  });
});

describe("formatTokens", () => {
  test("rounds powers of 1024 to a K figure", () => {
    expect(formatTokens(65536)).toBe("64K");
    expect(formatTokens(131072)).toBe("128K");
  });

  test("leaves counts under 1024 as plain numbers", () => {
    expect(formatTokens(512)).toBe("512");
  });
});

describe("scannedModel", () => {
  const servers = [
    server("http://localhost:1234/v1", [localModel("a"), localModel("b")]),
  ];

  test("finds a model on the server whose URL matches", () => {
    expect(scannedModel(servers, "http://localhost:1234/v1", "b")?.id).toBe(
      "b",
    );
  });

  test("ignores trailing slashes on either side of the URL comparison", () => {
    expect(scannedModel(servers, "http://localhost:1234/v1/", "a")?.id).toBe(
      "a",
    );
    expect(
      scannedModel(
        [server("http://localhost:1234/v1//", [localModel("a")])],
        "http://localhost:1234/v1",
        "a",
      )?.id,
    ).toBe("a");
  });

  test("an unknown server yields undefined", () => {
    expect(
      scannedModel(servers, "http://localhost:9999/v1", "a"),
    ).toBeUndefined();
  });

  test("an unknown model on a known server yields undefined", () => {
    expect(
      scannedModel(servers, "http://localhost:1234/v1", "zzz"),
    ).toBeUndefined();
  });
});

describe("chatChoices", () => {
  const url = "http://localhost:1234/v1";

  test("drops models on endpoints whose last probe was not ok", () => {
    const endpoints = [
      endpoint({ id: "good" }),
      endpoint({ id: "bad", lastStatus: "error" }),
      endpoint({ id: "untested", lastStatus: null }),
    ];
    const groups = [
      group("good", ["m1"]),
      group("bad", ["m2"]),
      group("untested", ["m3"]),
    ];
    expect(chatChoices(groups, endpoints, [])).toEqual([
      { endpointId: "good", model: "m1" },
    ]);
  });

  test("drops models the scan identified as embeddings", () => {
    const servers = [
      server(url, [
        localModel("chat", { kind: "llm" }),
        localModel("embed", { kind: "embeddings" }),
      ]),
    ];
    expect(
      chatChoices([group("ep1", ["chat", "embed"])], [endpoint()], servers),
    ).toEqual([{ endpointId: "ep1", model: "chat" }]);
  });

  test("keeps models the scan never saw", () => {
    expect(
      chatChoices([group("ep1", ["unscanned"])], [endpoint()], [server(url)]),
    ).toEqual([{ endpointId: "ep1", model: "unscanned" }]);
  });

  test("drops groups whose endpoint is not in the endpoint list", () => {
    expect(chatChoices([group("ghost", ["m1"])], [endpoint()], [])).toEqual([]);
  });
});

describe("preferredChoice", () => {
  const url = "http://localhost:1234/v1";
  const choices = [
    { endpointId: "ep1", model: "a" },
    { endpointId: "ep1", model: "b" },
  ];

  test("the model the scan reports loaded wins over order", () => {
    const servers = [
      server(url, [localModel("a"), localModel("b", { loaded: true })]),
    ];
    expect(preferredChoice(choices, [endpoint()], servers)).toEqual(choices[1]);
  });

  test("without a loaded model the first choice is preselected", () => {
    expect(preferredChoice(choices, [endpoint()], [server(url)])).toEqual(
      choices[0],
    );
  });

  test("a model reported not loaded does not win", () => {
    const servers = [
      server(url, [localModel("a", { loaded: false }), localModel("b")]),
    ];
    expect(preferredChoice(choices, [endpoint()], servers)).toEqual(choices[0]);
  });

  test("no choices yields null", () => {
    expect(preferredChoice([], [endpoint()], [server(url)])).toBeNull();
  });
});

describe("unclaimedServers", () => {
  test("a server no endpoint points at is returned", () => {
    const free = server("http://localhost:11434/v1");
    expect(unclaimedServers([free], [endpoint()])).toEqual([free]);
  });

  test("a server whose URL matches an endpoint modulo a trailing slash is excluded", () => {
    const claimed = server("http://localhost:1234/v1/");
    const free = server("http://localhost:11434/v1");
    expect(unclaimedServers([claimed, free], [endpoint()])).toEqual([free]);
  });
});
