import { Show, splitProps, type JSX } from "solid-js";
import { Dynamic } from "solid-js/web";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon, type IconProps } from "../primitives/Icon";

export interface TileProps {
  /** Big mono glyph/letter top-left (e.g. "O"). */
  glyph?: string;
  /** Or an icon name instead of a glyph. */
  icon?: IconProps["name"];
  /** Name shown bottom-left as a label. */
  label: string;
  selected?: boolean;
  locked?: boolean;
  href?: string;
  onClick?: () => void;
  class?: string;
}

/** Square nav cell with a large glyph and a label (§10.6): a surface fill inside a
 *  1px `line` rule, no radius, no shadow. Hover is the raised fill; selected is the
 *  raised fill plus the 2px bright inset edge. Locked = dim, non-interactive. */
export function Tile(props: TileProps): JSX.Element {
  const [local] = splitProps(props, [
    "glyph",
    "icon",
    "label",
    "selected",
    "locked",
    "href",
    "onClick",
    "class",
  ]);
  const tag = () => (local.locked ? "div" : local.href ? "a" : "button");
  return (
    <Dynamic
      component={tag()}
      href={!local.locked ? local.href : undefined}
      onClick={!local.locked ? local.onClick : undefined}
      aria-disabled={local.locked || undefined}
      class={cx(
        "group flex aspect-square flex-col justify-between border border-line bg-surface p-3 text-left transition-colors",
        local.selected && "heph-selected-edge bg-raised",
        local.locked ? "cursor-not-allowed text-dim" : "hover:bg-raised",
        local.class,
      )}
    >
      <div class="flex items-start justify-between">
        <Show
          when={local.glyph}
          fallback={
            local.icon && (
              <Icon
                name={local.icon}
                size={24}
                class={local.selected ? "text-bright" : "text-text"}
              />
            )
          }
        >
          <Text
            variant="readout-lg"
            tone={local.selected ? "bright" : "default"}
          >
            {local.glyph}
          </Text>
        </Show>
        <Show when={local.locked}>
          <Icon name="lock" class="text-dim" />
        </Show>
      </div>
      <Text variant="label" tone={local.selected ? "bright" : "dim"}>
        {local.label}
      </Text>
    </Dynamic>
  );
}
