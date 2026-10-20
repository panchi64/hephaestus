/**
 * The chat seam — everything outside this folder reaches the backend through here.
 *
 * A barrel rather than a module: the parts behind it each have their own reason to change
 * (what the wire looks like, what a payload means, how a conversation is read, how the
 * thread list is ordered, how a run is driven), and they used to share one 2700-line file
 * where none of that was visible. What the rest of the app imports has not moved, which is
 * the point — the split is internal, so a component still says `from "../data"` and gets
 * the same names.
 */

export { CONTINUE_PROMPT } from "./constants";

export {
  entrySessionId,
  isPinned,
  isWarm,
  orderSessions,
  pinnedIds,
  refreshSessions,
  RESUME_WINDOW_MS,
  togglePin,
  useChatSessions,
  useSessionArrivals,
} from "./sessions";

export { setWorkspaceOpen, workspaceOpen } from "./workspaceCollapse";

export { REVEAL_SPEED_MS, titleReveals } from "./titleReveals";

export {
  consumePendingDraft,
  consumeRequestedSession,
  openConversation,
  startConversation,
} from "./entry";

export { formatArgs } from "./messages";

export {
  conversationGrantsRevision,
  deleteConversation,
  fetchAutoCompactOverride,
  fetchGrants,
  fetchPlan,
  forkConversation,
  regenerateTitle,
  renameConversation,
  revokeGrant,
  setAutoCompactOverride,
  useChatSession,
} from "./conversations";

export {
  closeBrowser,
  createBrowserInput,
  fetchBrowserSession,
  openBrowser,
  streamBrowserView,
  type BrowserFrame,
  type BrowserSession,
  type BrowserViewEnd,
  type BrowserViewEvent,
} from "./browser";

export type { BrowserInput } from "./browserInput";

export {
  discardBranch,
  fetchBranch,
  mergeBranch,
  type BranchState,
  type FileChange,
} from "./branch";

export {
  fetchChangeDiff,
  fetchChanges,
  pendingCount,
  revertChanges,
  type ChangedFile,
  type ProjectChanges,
  type RevertReport,
  type ThreadChanges,
} from "./changes";

export {
  fetchOwnCallsign,
  fetchSubagents,
  isLive,
  openSubagentThread,
  stopSubagent,
  designationText,
  type Subagent,
  type SubagentsRead,
  type SubagentStatus,
} from "./subagents";

export {
  fetchSnapshotDiffs,
  fetchSnapshotFiles,
  fetchSnapshotFileText,
  snapshotFilePath,
} from "./snapshots";

export { createChatStream, type ChatStreamOptions } from "../stream/chatStream";

export { chatDraftKey, mainChat, type MainChat } from "../mainChat";
