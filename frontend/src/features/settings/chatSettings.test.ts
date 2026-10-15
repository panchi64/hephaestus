import { describe, expect, test } from "bun:test";
import { toChatSettings, toChatSettingsBody } from "./chatSettingsDto";

/**
 * The chat-settings wire mapping, in the one place it can go wrong quietly.
 *
 * A PUT here sends only the keys the caller touched, and "touched" is decided by an
 * `!== undefined` test rather than by truthiness. That distinction is invisible until a
 * setting has a meaningful falsy value — and two do: a `null` wall clock or `null`
 * sub-agent cap is the value that removes the bound. Written as `if (patch.x)`, both
 * would be dropped on the floor with no error anywhere, and only for the operators who
 * picked them.
 */

const DTO = {
  auto_compact_enabled: true,
  auto_compact_threshold: 0.8,
  work_summary_idle_minutes: 15,
  context_warn_threshold: 0.75,
  context_alert_threshold: 0.9,
  agent_request_limit: 25,
  auto_continue_max: 0,
  first_token_timeout_s: 600,
  inactivity_timeout_s: 120,
  wall_clock_timeout_s: null,
  subagent_max_concurrent: null,
};

describe("reading the stored preferences", () => {
  test("every field arrives under its camelCase name", () => {
    expect(toChatSettings(DTO)).toEqual({
      autoCompactEnabled: true,
      autoCompactThreshold: 0.8,
      workSummaryIdleMinutes: 15,
      contextWarnThreshold: 0.75,
      contextAlertThreshold: 0.9,
      agentRequestLimit: 25,
      autoContinueMax: 0,
      firstTokenTimeoutS: 600,
      inactivityTimeoutS: 120,
      wallClockTimeoutS: null,
      subagentMaxConcurrent: null,
    });
  });
});

describe("writing a patch", () => {
  test("only the touched keys are sent", () => {
    expect(toChatSettingsBody({ autoCompactThreshold: 0.6 })).toEqual({
      auto_compact_threshold: 0.6,
    });
  });

  test("removing the wall clock survives the encode", () => {
    // The trap this whole file exists for.
    expect(toChatSettingsBody({ wallClockTimeoutS: null })).toEqual({
      wall_clock_timeout_s: null,
    });
  });

  test("removing the sub-agent cap survives the encode", () => {
    // `null` is the value that lifts the cap, and it is falsy — the same trap as the wall
    // clock, in the second field to carry it.
    expect(toChatSettingsBody({ subagentMaxConcurrent: null })).toEqual({
      subagent_max_concurrent: null,
    });
  });

  test("turning auto-continue off survives the encode", () => {
    // 0 is a setting here (stop at the limit every time), and it is falsy.
    expect(toChatSettingsBody({ autoContinueMax: 0 })).toEqual({
      auto_continue_max: 0,
    });
  });

  test("an untouched key is absent rather than sent as a default", () => {
    // Absence is what tells the backend to leave a field alone, so a mapper that filled
    // in defaults would overwrite settings the operator never opened.
    expect(toChatSettingsBody({ autoCompactEnabled: false })).toEqual({
      auto_compact_enabled: false,
    });
  });
});
