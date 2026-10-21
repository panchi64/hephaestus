import type { ApprovalCautions } from "./model";

/** Whether an approval may offer a standing grant. A guarded call always asks, so it may
 *  not. */
export const offersGrant = (cautions: ApprovalCautions): boolean =>
  !cautions.guard;

export interface CautionLine {
  /** The mono word that names the caution. */
  label: string;
  /** The backend's own sentence. */
  reason: string;
}

/** The lines an approval card prints for what sets it apart, guard first. */
export function cautionLines(cautions: ApprovalCautions): CautionLine[] {
  const lines: CautionLine[] = [];
  if (cautions.guard)
    lines.push({ label: "Always asks", reason: cautions.guard });
  if (cautions.unconfined)
    lines.push({ label: "Unconfined", reason: cautions.unconfined });
  return lines;
}
