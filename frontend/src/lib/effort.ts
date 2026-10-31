/** Effort levels an endpoint or a thread can pin, as the backend's
 *  `services.reasoning.EFFORTS` names them. "" is no pin: the next layer's level applies. */
export const EFFORT_LEVELS = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra high" },
  { value: "max", label: "Max" },
] as const;

/** The level an Anthropic endpoint runs at when nothing is pinned. */
export const DEFAULT_EFFORT = "high";

/** The label for a stored level, or the raw value when it isn't one of ours. */
export function effortLabel(value: string): string {
  return EFFORT_LEVELS.find((level) => level.value === value)?.label ?? value;
}

/** The select options: "Default (<fallback>)" first, then every level. */
export function effortOptions(
  fallback: string,
): { value: string; label: string }[] {
  return [
    { value: "", label: `Default (${effortLabel(fallback).toLowerCase()})` },
    ...EFFORT_LEVELS.map((level) => ({ ...level })),
  ];
}
