/** Every asset id the interface prints, in one `HEPH-<SUBSYSTEM>-<SEQ>` scheme (§11).
 *  Declare an id here, never inline at its call site. */
export const ASSET_IDS = {
  workspaceRail: "HEPH-WKS-01",
  setupRail: "HEPH-SET-01",
  auth: "HEPH-AUTH-01",
  notFound: "HEPH-ERR-404",
  knowledgeBase: "HEPH-RAG-01",
  compare: "HEPH-CMP-01",
  mail: "HEPH-MAIL-01",
  calendar: "HEPH-CAL-01",
  routines: "HEPH-RTN-01",
} as const;

export type AssetId = (typeof ASSET_IDS)[keyof typeof ASSET_IDS];
