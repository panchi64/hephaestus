import { describe, expect, test } from "bun:test";
import { approvalFromEvent, type ApprovalEvent } from "./approvalEvent";

const event = (over: Partial<ApprovalEvent> = {}): ApprovalEvent => ({
  type: "approval.required",
  seq: 1,
  ts: "",
  tool_call_id: "t1",
  name: "mail_send",
  args: {},
  summary: "Send mail",
  explanation: null,
  ...over,
});

describe("an approval card from its event", () => {
  test("a guard copied into the explanation is carried once", () => {
    const card = approvalFromEvent(
      event({ guard: "sends mail", explanation: "sends mail" }),
    );
    expect(card.guard).toBe("sends mail");
    expect(card.explanation).toBeUndefined();
  });

  test("a tool's own explanation is kept beside the guard", () => {
    const card = approvalFromEvent(
      event({ guard: "sends mail", explanation: "Mails the report" }),
    );
    expect(card.explanation).toBe("Mails the report");
  });

  test("the unconfined reason and missing args are carried and defaulted", () => {
    const card = approvalFromEvent(
      event({
        unconfined: "no fence",
        args: undefined as unknown as Record<string, unknown>,
      }),
    );
    expect(card.unconfined).toBe("no fence");
    expect(card.args).toEqual({});
  });
});
