/** Pure helpers shared by the Skills and Agents screens — both are a name and
 *  description in frontmatter over a markdown body, with a draft/published
 *  lifecycle, so the directory, the read view and the editor are one design. */

export const LIBRARY_TABS = [
  { value: "all", label: "All" },
  { value: "published", label: "Published" },
  { value: "draft", label: "Draft" },
] as const;
export const LIBRARY_TAB_VALUES = ["all", "published", "draft"] as const;
export type LibraryTab = (typeof LIBRARY_TAB_VALUES)[number];

export interface LibraryEntry {
  id: string;
  name: string;
  description: string;
  published: boolean;
}

/** A headed section of directory rows. */
export interface LibraryGroup<T> {
  key: string;
  label: string;
  note: string;
  items: T[];
}

/** The rows a tab shows. Published and draft partition the list. */
export function filterByTab<T extends { published: boolean }>(
  items: readonly T[],
  tab: LibraryTab,
): T[] {
  if (tab === "all") return [...items];
  return items.filter((item) => item.published === (tab === "published"));
}

/** What the directory's search box matches against. */
export function searchText(item: {
  name: string;
  description: string;
}): string {
  return `${item.name} ${item.description}`;
}

export function compareByName(
  a: { name: string },
  b: { name: string },
): number {
  return a.name.localeCompare(b.name);
}

export function compareByUpdated(
  a: { updatedAt: string },
  b: { updatedAt: string },
): number {
  return a.updatedAt.localeCompare(b.updatedAt);
}

/** `"12 / 64"` under a length-limited field; just `"12"` where there is no limit. */
export function charCount(value: string, max?: number): string {
  return max === undefined ? String(value.length) : `${value.length} / ${max}`;
}

/* ── Contents tree ────────────────────────────────────────────────────────── */

export type TreeNode =
  | { kind: "file"; name: string; path: string }
  | { kind: "dir"; name: string; path: string; children: TreeNode[] };

/** Paths such as `references/examples.md` folded into folders. The `main` file
 *  is listed first and loose; folders follow, then the remaining files, each
 *  group alphabetical. */
export function buildFileTree(
  main: string,
  paths: readonly string[],
): TreeNode[] {
  const root: TreeNode[] = [];
  for (const path of paths) {
    const parts = path.split("/").filter(Boolean);
    let level = root;
    parts.forEach((part, i) => {
      const at = parts.slice(0, i + 1).join("/");
      if (i === parts.length - 1) {
        level.push({ kind: "file", name: part, path: at });
        return;
      }
      let dir = level.find((n) => n.kind === "dir" && n.name === part);
      if (!dir) {
        dir = { kind: "dir", name: part, path: at, children: [] };
        level.push(dir);
      }
      if (dir.kind === "dir") level = dir.children;
    });
  }
  sortTree(root);
  return [{ kind: "file", name: main, path: main }, ...root];
}

function sortTree(nodes: TreeNode[]): void {
  nodes.sort((a, b) =>
    a.kind === b.kind
      ? a.name.localeCompare(b.name)
      : a.kind === "dir"
        ? -1
        : 1,
  );
  for (const n of nodes) if (n.kind === "dir") sortTree(n.children);
}

export function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

/* ── Frontmatter ──────────────────────────────────────────────────────────── */

const PLAIN_SCALAR = /^[A-Za-z0-9_./][A-Za-z0-9_./ -]*$/;

function scalar(value: string): string {
  return PLAIN_SCALAR.test(value) &&
    !/^(true|false|null|yes|no|\d.*)$/i.test(value)
    ? value
    : JSON.stringify(value);
}

function yamlValue(value: unknown): string {
  if (typeof value === "string") return scalar(value);
  return JSON.stringify(value);
}

/** A `---` frontmatter block followed by the body. Keys with no value are
 *  left out; list and object values are written as inline JSON, which YAML
 *  reads as the same flow collections. */
export function composeMarkdownFile(
  fields: readonly (readonly [string, unknown])[],
  body: string,
): string {
  const lines = fields
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .filter(([, v]) => !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}: ${yamlValue(v)}`);
  return `---\n${lines.join("\n")}\n---\n\n${body}`;
}

/** The body of a markdown file, frontmatter removed — for rendering a preview
 *  of a file whose source arrived whole. */
export function stripFrontmatter(text: string): string {
  const match = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(text);
  return match ? text.slice(match[0].length).replace(/^\r?\n/, "") : text;
}

/** How many leading lines are frontmatter, fences included; 0 when the text
 *  does not open with a `---` fence that a later `---` closes. */
export function frontmatterLineCount(text: string): number {
  const lines = text.split("\n");
  if (lines[0]?.trimEnd() !== "---") return 0;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trimEnd() === "---") return i + 1;
  }
  return 0;
}

const TEXT_EXTENSIONS =
  /\.(md|markdown|txt|json|ya?ml|toml|csv|html?|css|xml|py|js|mjs|ts|sh|rst)$/i;

/** Whether a bundle file is plain text the editor can round-trip. */
export function isTextPath(path: string): boolean {
  return TEXT_EXTENSIONS.test(path);
}
