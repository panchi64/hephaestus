import type { MarkedExtension } from "marked";
import { hostLabel } from "~/lib/format";
import { localImageRef } from "./localImages";

/**
 * Anchors for the `marked` pipeline (see Markdown.tsx) — the single place a link
 * in rendered prose is minted.
 *
 * Links are a *feature*: the model is told (prompts/agent.py) that Markdown link
 * syntax renders, so it can put a page it actually visited behind the words that
 * describe it instead of trailing a bare URL. That makes every anchor on screen
 * model-authored, and a model that just read a web page is relaying untrusted
 * text — so this module is also the chokepoint that makes those anchors safe:
 *
 *   • `link` — scheme-allowlisted (`http`/`https`/`mailto`), attribute-escaped,
 *     and opened in a new tab with the opener severed. A rejected href degrades
 *     to the link's own text rather than to a dead or dangerous anchor.
 *   • `link`, second form — a **path** rather than a URL, which the model is
 *     told to write when it is pointing the operator at a file of theirs. It is
 *     not a destination and never becomes an `href`: it is parked in
 *     `data-open-path` for Markdown.tsx's delegated handler to POST to
 *     `/host/open`, which refuses anything outside the operator's own project
 *     roots *and* anything inside them the host would run rather than show.
 *     Given a path is model-authored text, the check here is only that it is a
 *     path *shape* at all — the fence is the backend's, deliberately, since a
 *     rule about which files may be opened cannot live in the browser: it is the
 *     only side that knows what a project root contains, what a name resolves to
 *     through a symlink, and whether the bit that makes a file a program is set.
 *   • `image` — emitted with its address parked in `data-remote-src` and no `src`
 *     at all, for Markdown.tsx to resolve through the backend proxy. An `<img>`
 *     is the one construct that *fetches the instant it has a src*, with no click
 *     to gate it, so a relayed `![](https://…/p.gif?id=…)` pointed straight at
 *     its host would be a tracking pixel loaded from the operator's own browser,
 *     carrying their address and cookies. The picture is worth showing; the
 *     request is not worth making from here. See services/webimage.py.
 *   • `html` — raw HTML in the source is escaped to visible text rather than
 *     injected. Prose here is Markdown; passing `marked`'s raw-HTML tokens
 *     through to `innerHTML` would hand an `<a href="javascript:…">` (or an
 *     `<img onerror>`) a way around the two renderers above, and there is
 *     nothing an answer needs to say that Markdown can't.
 */

/** Schemes an anchor may carry. Everything else — `javascript:`, `data:`,
 *  `vbscript:`, `file:`, protocol-relative `//host` — is rejected: an anchor in
 *  prose is outbound, and a href that resolves *relative to the app* would
 *  navigate it off its own route on a click. A path is not an exception to that;
 *  it is the other form entirely (see `workspacePath`), and it never becomes an
 *  href at all. */
const ALLOWED_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/** Named entities that decode to characters a scheme can hide behind. The
 *  numeric forms are handled by pattern; these are the named ones that matter. */
const NAMED_ENTITIES: Record<string, string> = {
  "&colon;": ":",
  "&tab;": "\t",
  "&newline;": "\n",
  "&amp;": "&",
  "&quot;": '"',
  "&apos;": "'",
  "&lt;": "<",
  "&gt;": ">",
};

/** Whitespace and C0/DEL controls, which URL parsing drops rather than honours —
 *  so `java\tscript:` is `javascript:` by the time it is a navigation. */
function stripIgnored(value: string): string {
  let out = "";
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0;
    if (code > 0x20 && code !== 0x7f) out += ch;
  }
  return out;
}

/** The href as the *browser* will see it once it parses the attribute: HTML
 *  entities resolved, then the characters URL parsing ignores removed.
 *  `java&Tab;script:` and `java\tscript:` both collapse to `javascript:` here,
 *  which is the point — the scheme check runs on this, never on the source text.
 *  Decoding repeats to a fixed point so a double-encoded entity can't survive a
 *  single pass. */
function decodedHref(href: string): string {
  let out = href;
  for (let i = 0; i < 4; i++) {
    const next = out
      .replace(/&#x([0-9a-f]+);?/gi, (m, hex: string) => {
        const code = parseInt(hex, 16);
        return Number.isFinite(code) && code <= 0x10ffff
          ? String.fromCodePoint(code)
          : m;
      })
      .replace(/&#(\d+);?/g, (m, dec: string) => {
        const code = parseInt(dec, 10);
        return Number.isFinite(code) && code <= 0x10ffff
          ? String.fromCodePoint(code)
          : m;
      })
      .replace(/&[a-z]+;/gi, (m) => NAMED_ENTITIES[m.toLowerCase()] ?? m);
    if (next === out) break;
    out = next;
  }
  return stripIgnored(out);
}

/** A URL scheme at the head of a value — any of them, not just an allowed one. */
const SCHEME_HEAD = /^([a-z][a-z0-9+.-]*):/i;

/** The allowlisted scheme at the head of `value`, or `null`. */
function allowedScheme(value: string): string | null {
  const m = SCHEME_HEAD.exec(value);
  if (!m) return null;
  const scheme = `${m[1].toLowerCase()}:`;
  return ALLOWED_SCHEMES.has(scheme) ? scheme : null;
}

/**
 * The href to put on an anchor, or `null` if this one may not become a link.
 *
 * The returned string is the *original* source href, not the decoded one: a
 * legitimate `?a=1&amp;b=2` has to reach the DOM as written so the browser
 * decodes it back into the URL the author meant.
 *
 * Which is why *both* forms have to clear the allowlist, not just the decoded
 * one. `escapeAttr` escapes the `&`, so the browser resolves the raw string and
 * never entity-decodes it — an href whose scheme only exists after decoding
 * (`&#104;ttps://…`) would be accepted here and then land in the DOM with no
 * scheme at all, resolving *relative to the app* and navigating off its own
 * route on a click. Requiring agreement rejects those, and still rejects
 * everything the decoded check was there to catch (`java&Tab;script:`), since
 * neither form clears the allowlist.
 */
export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  const raw = allowedScheme(stripIgnored(href));
  return raw !== null && raw === allowedScheme(decodedHref(href)) ? href : null;
}

/** Whether a value carries a C0/DEL control. A path may hold a space — a folder
 *  called `My Notes` is not a mistake — but never one of these: they are what the
 *  browser drops rather than shows, so the string on screen would not be the
 *  string acted on. Written as a scan rather than a regex literal, which would
 *  need a control-character escape and the lint suppression that comes with it. */
function hasControls(value: string): boolean {
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0;
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

/** A Windows drive prefix (`C:/…`, `C:\…`), which reads as a one-letter scheme
 *  to the check above and is a perfectly ordinary absolute path on that host. */
const DRIVE_PREFIX = /^[a-z]:[\\/]/i;

/** Path shape: something with a separator in it, or a bare filename carrying an
 *  extension. Without this, `[click](here)` would mint a control that can only
 *  ever fail — a word is not a file. */
const PATH_SHAPE = /[\\/]/;
const FILENAME_SHAPE = /^[^\s/\\]+\.[a-z0-9]{1,10}$/i;

/**
 * The workspace path an href names, or `null` if this one is not one.
 *
 * The complement of {@link safeHref}: that function's job is to reject anything
 * without an outbound scheme, and this one picks up exactly what it dropped —
 * because a path in an answer is no longer a model mistake. It is how the model
 * points at a file, and clicking it opens that file on the operator's machine.
 *
 * What is rejected here is *URL grammar wearing a path's clothes*, not unsafe
 * files — a leading `//` (or `\\`) names a host rather than a path, a scheme
 * means it was a URL the allowlist already refused, and `?`/`#` are a query and
 * a fragment. Control characters go for the same reason they do above: what the
 * browser ignores when parsing is not what the eye read.
 *
 * Which files may actually be opened is decided by the backend — against the
 * operator's project roots, and against what the host would *execute* rather
 * than show — and it has to be: the browser cannot know what is inside a root or
 * what a name resolves to on that filesystem, and a fence drawn in a renderer is
 * not a fence. Screening suffixes here too would be the worse kind of duplicate:
 * a second policy, drifting from the real one, and read as protection.
 */
export function workspacePath(href: string | null | undefined): string | null {
  const raw = (href ?? "").trim();
  if (!raw || hasControls(raw)) return null;
  if (/^[/\\]{2}/.test(raw)) return null;
  if (/[?#]/.test(raw)) return null;
  if (!DRIVE_PREFIX.test(raw)) {
    // Both forms, for the reason `safeHref` checks both: a scheme that only
    // appears once entities are decoded is still a scheme.
    if (SCHEME_HEAD.test(raw) || SCHEME_HEAD.test(decodedHref(raw)))
      return null;
  }
  return PATH_SHAPE.test(raw) || FILENAME_SHAPE.test(raw) ? raw : null;
}

/** Whether an accepted href leaves the app — everything but `mailto:`, which
 *  hands off to a mail client and must not open a blank tab to do it. */
export function isExternalHref(href: string): boolean {
  return !/^mailto:/i.test(decodedHref(href));
}

/** Escape for an HTML *attribute* value — the same five characters `marked`
 *  escapes when it renders text, which is what lets `anchorHtml` compare a
 *  link's rendered text against its href. `&` first, or it would re-escape the
 *  ampersands the later replacements introduce. */
function escapeAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Escape for HTML *text* content. */
function escapeText(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * The anchor markup for one accepted link.
 *
 * `target`/`rel` are set here rather than patched onto the DOM afterwards, so an
 * anchor is never briefly live with the wrong ones and the rendered HTML is
 * already correct when it is cached (Markdown.tsx caches parsed blocks by
 * source).
 *
 * `title` names the destination on hover — the operator's "where does this go?"
 * before committing a click: the base domain the Sources row shows for a web
 * link, and the address itself for a `mailto:`, which has no host to name and no
 * `↗` to warn with. A title the author wrote wins, and a link whose text is
 * already its URL gets none, since it would only repeat itself.
 */
function anchorHtml(
  href: string,
  inner: string,
  markdownTitle?: string | null,
): string {
  const external = isExternalHref(href);
  const host = hostLabel(href);
  const destination = external ? host : href.trim().replace(/^mailto:/i, "");
  const title =
    markdownTitle?.trim() ||
    // Against `escapeAttr`, not `escapeText`: `marked` escapes quotes in the
    // rendered text too, so an autolinked URL containing one has to be compared
    // in the same alphabet or it never matches itself.
    (inner.trim() === escapeAttr(href.trim()) ? "" : destination);
  return [
    `<a href="${escapeAttr(href)}"`,
    external ? ` target="_blank" rel="noopener noreferrer"` : "",
    external ? ` data-link-external="${escapeAttr(host)}"` : "",
    title ? ` title="${escapeAttr(title)}"` : "",
    `>${inner}</a>`,
  ].join("");
}

/**
 * The control for a path the answer pointed at.
 *
 * A `<button>`, not an anchor, and the distinction is the point: this navigates
 * nowhere, it asks the machine the browser is running on to open a file. It gets
 * keyboard focus and Enter/Space for free by being the element it actually is,
 * which an `<a>` with no `href` would have had to fake with `tabindex`, `role`
 * and a keydown handler of its own — three chances to get accessibility wrong in
 * exchange for a tag name.
 *
 * The `title` says what the click does, since — unlike a web link, whose host
 * answers "where does this go?" — the destination here *is* the visible text.
 */
function openPathHtml(
  path: string,
  inner: string,
  markdownTitle?: string | null,
): string {
  const title = markdownTitle?.trim() || `Open ${path}`;
  return [
    `<button type="button" class="heph-open-path"`,
    ` data-open-path="${escapeAttr(path)}"`,
    ` title="${escapeAttr(title)}"`,
    `>${inner}</button>`,
  ].join("");
}

/** Pass to `marked.use(...)` to mint safe anchors and neutralise raw HTML. */
export const markedLinks: MarkedExtension = {
  renderer: {
    link(token) {
      const inner = this.parser.parseInline(token.tokens);
      const href = safeHref(token.href);
      if (href !== null) return anchorHtml(href, inner, token.title);
      const path = workspacePath(token.href);
      // A rejected href leaves the sentence intact and drops the anchor: the
      // words the model wrote still read, they just don't go anywhere.
      return path === null ? inner : openPathHtml(path, inner, token.title);
    },
    image(token) {
      const alt = token.text ?? "";
      const local = localImageRef(token.href);
      if (local !== null)
        return [
          `<img data-local-ref="${escapeAttr(local)}"`,
          ` alt="${escapeAttr(alt)}"`,
          ` class="heph-local-image">`,
        ].join("");
      const href = safeHref(token.href);
      // An image the operator can't see is worth its caption; an image we can't
      // vouch for is worth nothing. `mailto:` is not an image address either.
      if (href === null || !isExternalHref(href)) return escapeText(alt);
      // Deliberately *no* `src`: an `<img>` fetches the moment it has one, and a
      // remote address here would be a silent request from the operator's browser
      // to a host named by relayed page content. The address is parked in a data
      // attribute and Markdown.tsx resolves it through the backend proxy instead
      // — same origin, no operator identity on the wire. See services/webimage.py.
      return [
        `<img data-remote-src="${escapeAttr(href)}"`,
        ` alt="${escapeAttr(alt)}"`,
        ` title="${escapeAttr(hostLabel(href))}"`,
        ` class="heph-remote-image">`,
      ].join("");
    },
    html(token) {
      // Comments carry nothing for the reader, and showing them escaped would be
      // noise, so they are dropped outright. Everything else becomes visible
      // text — output that looks wrong is a far better failure than injection.
      if (/^\s*<!--/.test(token.text)) return "";
      return escapeText(token.text);
    },
  },
};
