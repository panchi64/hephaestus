/**
 * Public surface of the design system. Everything outside `src/ui` imports
 * components and types from here (`~/ui`), never from deep paths.
 *
 * Rules (see src/ui/CLAUDE.md):
 *  - tokens.css is the single source of truth for colors/spacing/type.
 *  - Cosmetic differences are variant props, never forked components.
 *  - No eased/decorative spinners — use LoadingText / EmptyState. (The one live
 *    "working" indicator is Frames: hard-stepped glyph cycling, see design §8.)
 */

// utils
export { cx, type ClassValue } from "./cx";
export { copyToClipboard } from "./clipboard";
export { REVEAL_BASE, REVEAL_ON_GROUP_HOVER } from "./reveal";

// theme
export {
  preference,
  setTheme,
  toggleTheme,
  applyTheme,
  resolveTheme,
  systemTheme,
  syncSystemTheme,
  DEFAULT_THEME,
  DEFAULT_PREFERENCE,
  THEME_STORAGE_KEY,
  THEME_CYCLE,
  type ThemeMode,
  type ThemePreference,
} from "./theme/theme-store";
export { useTheme } from "./theme/useTheme";
export { ThemeProvider } from "./theme/ThemeProvider";
export {
  ACCENT_TOKENS,
  ACCENT_DEFAULTS,
  type AccentToken,
  type AccentTokenSpec,
} from "./theme/accents";
export {
  accentOverrides,
  hasAccentOverrides,
  resetAllAccents,
  restoreAccents,
  serializeOverrides,
  ACCENT_STORAGE_KEY,
  type AccentOverrides,
} from "./theme/accent-overrides";
export {
  accentValue,
  isAccentOverridden,
  resetAccent,
  setAccent,
} from "./theme/accent-store";
export {
  accentContrast,
  contrastRatio,
  meetsAccentFloor,
  normalizeHex,
  relativeLuminance,
  ACCENT_CONTRAST_FLOOR,
  MODE_BG,
} from "./theme/contrast";

// primitives
export { Box } from "./primitives/Box";
export {
  Text,
  type TextProps,
  type TextVariant,
  type TextTone,
} from "./primitives/Text";
export { Stack, type StackProps, type GapStep } from "./primitives/Stack";
export { Row, type RowProps } from "./primitives/Row";
export { Icon, type IconProps } from "./primitives/Icon";
export {
  useFileDrop,
  HIDDEN_FILE_INPUT,
  type FileDropApi,
} from "./primitives/useFileDrop";
export { type IconName } from "./icons/registry";

// spec components
export { Panel, type PanelProps, type PanelState } from "./components/Panel";
export { Field, type FieldProps } from "./components/Field";
export { Readout, type ReadoutProps } from "./components/Readout";
export {
  StatusFlag,
  type StatusFlagProps,
  type Status,
} from "./components/StatusFlag";
export { StatusDot, type StatusDotProps } from "./components/StatusDot";
export {
  InstrumentBand,
  type InstrumentBandProps,
  type BandCell,
} from "./components/InstrumentBand";
// The mission-control register (§10.14–10.16, §11.1). A console group is the frame
// the other two sit in; the deep field is the one expressive device per screen, and
// belongs only in a region with nothing to compete with.
export {
  ConsoleGroup,
  type ConsoleGroupProps,
} from "./components/ConsoleGroup";
export {
  AnnunciatorGrid,
  type AnnunciatorGridProps,
  type AnnunciatorCell,
} from "./components/AnnunciatorGrid";
export { MetClock, type MetClockProps } from "./components/MetClock";
export { DeepField, type DeepFieldProps } from "./components/DeepField";
export { Tile, type TileProps } from "./components/Tile";
export { Chip, type ChipProps } from "./components/Chip";
export {
  AttachmentChip,
  type AttachmentChipProps,
  type ComposerAttachment,
  type AttachmentStatus,
} from "./components/AttachmentChip";
// The action tier: anything waiting on the operator (an approval, a question, a
// conflict) gets a filled plate band, a tinted body and a filled primary.
export {
  ActionFrame,
  ActionButtons,
  type ActionFrameProps,
  type ActionTone,
  type ActionButtonsProps,
  type ActionButtonSpec,
} from "./components/ActionFrame";
export {
  ChoiceList,
  type ChoiceListProps,
  type ChoiceOption,
} from "./components/ChoiceList";
export {
  ScrollFade,
  type ScrollFadeProps,
  type ScrollFadeEdge,
  type ScrollFadeVariant,
  type ScrollFadeGround,
} from "./components/ScrollFade";
export { ListRow, type ListRowProps } from "./components/ListRow";
export {
  RailRow,
  RailTag,
  type RailRowProps,
  type RailRowState,
} from "./components/RailRow";
export {
  ListGroupHeader,
  type ListGroupHeaderProps,
} from "./components/ListGroupHeader";
export { ListToolbar, type ListToolbarProps } from "./components/ListToolbar";
export {
  RegistrationFrame,
  type RegistrationFrameProps,
} from "./components/RegistrationFrame";
export { ImageFrame, type ImageFrameProps } from "./components/ImageFrame";

// controls
export {
  Button,
  type ButtonProps,
  type ButtonVariant,
  type ButtonSize,
} from "./components/Button";
export { Input, type InputProps } from "./components/Input";
export { PathInput, type PathInputProps } from "./components/PathInput";
export { Textarea, type TextareaProps } from "./components/Textarea";
export {
  ExternalLink,
  type ExternalLinkProps,
} from "./components/ExternalLink";
export { Composer } from "./components/composer/Composer";
export type {
  ComposerProps,
  ComposerAttachmentsApi,
  ComposerMenuApi,
  ComposerRecallApi,
} from "./components/composer/types";
export { moveDraft } from "./components/composer/composerDraft";
export {
  ComposerMenu,
  type ComposerMenuProps,
  type ComposerMenuGroup,
  type ComposerMenuItem,
} from "./components/composer/ComposerMenu";
export {
  tokenAt,
  replaceToken,
  type ComposerToken,
  type ComposerTrigger,
} from "./components/composer/composerToken";
export {
  Markdown,
  markdownBlocks,
  type MarkdownProps,
} from "./components/Markdown";
export { CodeBlock, type CodeBlockProps } from "./components/CodeBlock";
export { DiffView, type DiffViewProps } from "./components/DiffView";
export { Caret, type CaretProps } from "./components/Caret";
export { Frames, type FramesProps } from "./components/Frames";
export {
  TypewriterText,
  type TypewriterTextProps,
} from "./components/TypewriterText";
export {
  Select,
  type SelectProps,
  type SelectOption,
} from "./components/Select";
export {
  Combobox,
  type ComboboxProps,
  type ComboboxGroup,
  type ComboboxOption,
} from "./components/Combobox";
export { Checkbox, type CheckboxProps } from "./components/Checkbox";
export {
  Disclosure,
  DisclosureToggle,
  type DisclosureMarker,
  type DisclosureProps,
  type DisclosureToggleProps,
} from "./components/Disclosure";
export { Toggle, type ToggleProps } from "./components/Toggle";
export { Tabs, type TabsProps, type TabItem } from "./components/Tabs";
export {
  Segmented,
  type SegmentedProps,
  type SegmentedOption,
} from "./components/Segmented";
export { Modal, type ModalProps } from "./components/Modal";
export { Drawer, type DrawerProps } from "./components/Drawer";
export {
  Lightbox,
  type LightboxProps,
  type LightboxItem,
} from "./components/Lightbox";
export { Tooltip, type TooltipProps } from "./components/Tooltip";
export { InfoHint, type InfoHintProps } from "./components/InfoHint";
export {
  ExpandableText,
  type ExpandableTextProps,
} from "./components/ExpandableText";
export { ColorField, type ColorFieldProps } from "./components/ColorField";
export {
  ConstructionReveal,
  type ConstructionRevealProps,
} from "./components/ConstructionReveal";
export {
  FramedOverlay,
  type FramedOverlayProps,
} from "./components/FramedOverlay";
export { useGatedMount, type GatedMount } from "./components/useGatedMount";
export { Menu, type MenuProps, type MenuItem } from "./components/Menu";
export {
  ContextMenu,
  createContextMenu,
  type ContextMenuApi,
  type ContextMenuProps,
  type ContextMenuTriggerProps,
} from "./components/ContextMenu";
export { MetaAction, type MetaActionProps } from "./components/MetaAction";
export {
  StatusBar,
  StatusCell,
  StatusField,
  type StatusBarProps,
  type StatusCellProps,
  type StatusFieldProps,
} from "./components/StatusBar";
export {
  Popover,
  type PopoverProps,
  type PopoverApi,
} from "./components/Popover";
export {
  RevealPopover,
  type RevealPopoverProps,
} from "./components/RevealPopover";
export { ThemeToggle } from "./components/ThemeToggle";

// state / utility
export { LoadingText, type LoadingTextProps } from "./components/LoadingText";
export { EmptyState, type EmptyStateProps } from "./components/EmptyState";
export { ErrorState, type ErrorStateProps } from "./components/ErrorState";
export {
  ErrorBoundary,
  type ErrorBoundaryProps,
} from "./components/ErrorBoundary";
export { Resource, type ResourceProps } from "./components/Resource";
export { EditorShell, type EditorShellProps } from "./components/EditorShell";
export {
  toast,
  Toaster,
  type ToastTone,
  type ToastAction,
  type ToastOptions,
} from "./components/Toast";
export {
  confirm,
  confirmChoice,
  ConfirmHost,
  type ConfirmTone,
  type ConfirmChoice,
  type ConfirmOptions,
  type ConfirmChoiceOptions,
} from "./components/Confirm";
export {
  ForbiddenView,
  type ForbiddenViewProps,
} from "./components/ForbiddenView";
export { ProgressBar, type ProgressBarProps } from "./components/ProgressBar";
export {
  ProgressRing,
  type ProgressRingProps,
} from "./components/ProgressRing";
export {
  Reveal,
  type RevealProps,
  type RevealMotion,
} from "./components/Reveal";
export { Collapse, type CollapseProps } from "./components/Collapse";
export { Divider, type DividerProps } from "./components/Divider";
export {
  ResizeHandle,
  type ResizeHandleProps,
  type ResizeOrientation,
} from "./components/ResizeHandle";
export { Marquee, type MarqueeProps } from "./components/Marquee";
export { PageHeader, type PageHeaderProps } from "./components/PageHeader";
export { NotConnectedOverlay } from "./components/NotConnectedOverlay";
