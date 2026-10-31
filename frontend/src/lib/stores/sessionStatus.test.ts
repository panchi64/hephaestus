import { describe, expect, it } from "bun:test";
import { classify, dbMissingFrom, type AuthStatus } from "./sessionStatus";

/** A backend answer, defaulting to the ordinary "set up and unlocked" case so each
 *  test states only the fact it is about. */
const status = (over: Partial<AuthStatus> = {}): AuthStatus => ({
  initialized: true,
  unlocked: true,
  auth_enabled: true,
  db_missing: false,
  workspace: "open",
  workspace_failure: null,
  ...over,
});

describe("classify", () => {
  it("sends a workspace with no key to first-run setup", () => {
    // Even while the vault reports unlocked and we hold a token: nothing has been
    // set up, so there is nothing to be inside of.
    expect(classify(status({ initialized: false }), true)).toBe(
      "uninitialized",
    );
  });

  it("locks when the backend's vault is locked", () => {
    expect(classify(status({ unlocked: false }), true)).toBe("locked");
  });

  it("stays locked until the encrypted workspace has opened", () => {
    expect(classify(status({ workspace: "opening" }), true)).toBe("locked");
    expect(
      classify(
        status({ workspace: "failed", workspace_failure: "no volume" }),
        true,
      ),
    ).toBe("locked");
  });

  it("needs a token only while the gate is enabled", () => {
    expect(classify(status(), false)).toBe("locked");
    expect(classify(status({ auth_enabled: false }), false)).toBe("unlocked");
  });

  it("unlocks when the vault is open and we hold a token", () => {
    expect(classify(status(), true)).toBe("unlocked");
  });
});

describe("dbMissingFrom", () => {
  it("warns when a key outlived its database", () => {
    expect(dbMissingFrom(status({ unlocked: false, db_missing: true }))).toBe(
      true,
    );
  });

  it("stays quiet on a fresh install, whatever the flag says", () => {
    // Nothing is set up, so there is no key that could have outlived anything —
    // the operator belongs on setup, not on a warning about a missing database.
    expect(
      dbMissingFrom(status({ initialized: false, db_missing: true })),
    ).toBe(false);
  });

  it("stays quiet when the backend never answered", () => {
    expect(dbMissingFrom(null)).toBe(false);
  });

  it("stays quiet on an ordinary locked workspace", () => {
    expect(dbMissingFrom(status({ unlocked: false }))).toBe(false);
  });
});
