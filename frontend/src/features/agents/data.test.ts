import { describe, expect, it } from "bun:test";
import {
  agentErrorField,
  agentErrorMessage,
  agentsPath,
  toAgent,
} from "./data";
import type { AgentOut } from "./data";

const dto: AgentOut = {
  id: "builtin:default",
  name: "default",
  description: "The default agent",
  instructions: "",
  endpoint_id: null,
  model: null,
  tools: null,
  deny_tools: ["bash"],
  permission_ceiling: "edit",
  workspace: "shared",
  wall_clock_timeout_s: null,
  can_delegate: true,
  max_depth: 2,
  delegable: false,
  source: "builtin",
  published: true,
  active: true,
  editable: false,
};

describe("toAgent", () => {
  it("renames the snake_case wire fields and keeps nulls as nulls", () => {
    const agent = toAgent(dto);
    expect(agent.denyTools).toEqual(["bash"]);
    expect(agent.permissionCeiling).toBe("edit");
    expect(agent.wallClockTimeoutS).toBeNull();
    expect(agent.tools).toBeNull();
    expect(agent.canDelegate).toBe(true);
    expect(agent.maxDepth).toBe(2);
    expect(agent.editable).toBe(false);
  });
});

describe("agentsPath", () => {
  it("adds the project only when there is one, encoded", () => {
    expect(agentsPath(undefined)).toBe("/agents");
    expect(agentsPath("p 1")).toBe("/agents?project_id=p%201");
  });
});

describe("errors", () => {
  it("uses the backend's detail and field, else the fallback", () => {
    const err = { status: 422, detail: "Name taken", field: "name" };
    expect(agentErrorMessage(err, "fallback")).toBe("Name taken");
    expect(agentErrorField(err)).toBe("name");
    expect(agentErrorMessage(new Error("x"), "fallback")).toBe("fallback");
    expect(agentErrorField(new Error("x"))).toBeNull();
  });
});
