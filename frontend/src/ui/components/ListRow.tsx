import { Show, splitProps, type JSX } from "solid-js";
import { Dynamic } from "solid-js/web";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon, type IconProps } from "../primitives/Icon";

export interface ListRowProps {
  /** Primary label, left-aligned. A plain string renders through the row's
   *  own label typography; pass JSX when the label needs a composite look
   *  (e.g. a secondary tone/size alongside the main text). */
  label: string | JSX.Element;
  /** Leading icon. */
  leading?: IconProps["name"];
  /** Right-aligned content: status flag, meta, icon. */
  right?: JSX.Element;
  /** Secondary line under the label (§6.1 — a dim `micro` value beneath its
   *  label). Turns the single-line row into a two-line one; use it where the
   *  label alone doesn't say what the row is. */
  description?: string;
  selected?: boolean;
  /** Render a leading selection checkbox (driven by `selected`). The row's
   *  `onClick` should toggle selection. Replaces the `leading` icon. */
  selectable?: boolean;
  /** DOM id. */
  id?: string;
  locked?: boolean;
  href?: string;
  onClick?: () => void;
  /** Draw a bottom hairline. Off by default (§7): a list is already read as a
   *  list by its rhythm and its hover fill, and a rule under every row is the
   *  single largest source of line-noise in a dense screen. Turn it on only for
   *  a genuine ruled data grid, where the rule aligns columns across rows. */
  divided?: boolean;
  /** `nav`: the rail's footer destination (see the branch below). */
  variant?: "default" | "nav";
  class?: string;
}

/** Single-line row: label left, optional status/icon right (§10.7). Separation
 *  comes from rhythm and the hover fill, not from a rule. Locked rows are dim
 *  with a lock glyph. */
export function ListRow(props: ListRowProps): JSX.Element {
  const [local] = splitProps(props, [
    "label",
    "leading",
    "right",
    "description",
    "selected",
    "selectable",
    "id",
    "locked",
    "href",
    "onClick",
    "divided",
    "variant",
    "class",
  ]);
  const interactive = () => !local.locked && (local.href || local.onClick);
  // The rail's footer destination: a 16px icon, the label, its meta, on a ruled row
  // whose selection is the raised fill plus a 2px bright inset edge (`.heph-rail-nav`).
  if (local.variant === "nav")
    return (
      <Dynamic
        component={local.href ? "a" : local.onClick ? "button" : "div"}
        id={local.id}
        href={local.href}
        onClick={local.onClick}
        aria-current={local.selected && local.href ? "page" : undefined}
        data-selected={local.selected ? "" : undefined}
        class={cx("heph-rail-nav w-full text-left", local.class)}
      >
        <Show when={local.leading} fallback={<span />}>
          <Icon name={local.leading!} size={16} />
        </Show>
        <Text
          variant="body"
          tone={local.selected ? "bright" : "default"}
          class="truncate"
        >
          {local.label}
        </Text>
        <span class="flex shrink-0 items-center gap-2">{local.right}</span>
      </Dynamic>
    );
  // Selectable rows stay a <div> (with the matching semantics) so their `right`
  // slot can hold real interactive content — a Menu, a copy button — without
  // nesting a <button> inside a <button> (invalid HTML / hydration warnings).
  const tag = () =>
    local.locked || local.selectable
      ? "div"
      : local.href
        ? "a"
        : local.onClick
          ? "button"
          : "div";
  return (
    <Dynamic
      component={tag()}
      id={local.id}
      href={!local.locked ? local.href : undefined}
      onClick={!local.locked ? local.onClick : undefined}
      role={local.selectable ? "button" : undefined}
      aria-pressed={local.selectable ? local.selected || false : undefined}
      tabindex={local.selectable && !local.locked ? 0 : undefined}
      onKeyDown={
        local.selectable && !local.locked
          ? (e: KeyboardEvent & { currentTarget: HTMLElement }) => {
              // Only the row itself toggles — keys aimed at a nested control
              // in the `right` slot (Menu, copy button) must not double-fire.
              if (e.target !== e.currentTarget) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                local.onClick?.();
              }
            }
          : undefined
      }
      aria-disabled={local.locked || undefined}
      class={cx(
        "flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors",
        local.divided && "border-b border-line",
        local.selected && "bg-raised",
        local.selected && !local.selectable && "heph-selected-edge",
        interactive() && "hover:bg-raised",
        local.selectable && !local.locked && "cursor-pointer",
        local.locked && "cursor-not-allowed",
        local.class,
      )}
    >
      <span class="flex min-w-0 items-center gap-2">
        <Show when={local.selectable}>
          <span
            class={cx(
              "flex size-4 shrink-0 items-center justify-center border transition-colors",
              local.selected
                ? "border-bright text-bright"
                : "border-line text-transparent",
            )}
            aria-hidden="true"
          >
            <Show when={local.selected}>
              <Icon name="check" size={12} />
            </Show>
          </span>
        </Show>
        <Show when={local.leading && !local.selectable}>
          <Icon name={local.leading!} class="text-dim" />
        </Show>
        <span class="flex min-w-0 flex-col gap-1">
          <Text
            variant="label"
            tone={local.locked ? "dim" : local.selected ? "bright" : "default"}
            class="truncate"
          >
            {local.label}
          </Text>
          <Show when={local.description}>
            <Text variant="caption" tone="dim" class="truncate">
              {local.description}
            </Text>
          </Show>
        </span>
      </span>
      <span
        class="flex shrink-0 items-center gap-2"
        onClick={
          // The right slot can hold its own controls (Menu, copy). On any
          // interactive row their clicks must not trigger the row itself —
          // stop the bubble so a selectable row doesn't toggle, and cancel the
          // default so a link row doesn't navigate out from under an open menu.
          interactive()
            ? (e) => {
                e.stopPropagation();
                if (local.href) e.preventDefault();
              }
            : undefined
        }
      >
        <Show when={local.right}>{local.right}</Show>
        <Show when={local.locked}>
          <Icon name="lock" size={12} class="text-dim" />
        </Show>
      </span>
    </Dynamic>
  );
}
