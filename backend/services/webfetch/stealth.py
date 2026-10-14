"""Make the controlled browser present as an ordinary user's Chrome.

A headless, automated browser leaks tells a real one doesn't — a ``HeadlessChrome``
user-agent, ``navigator.webdriver === true``, an empty plugin list, a SwiftShader WebGL
renderer, and — the one a patched UA string misses — **client hints**: the headless shell
advertises a ``Sec-CH-UA`` brand of ``HeadlessChrome`` on every request and exposes the
same through ``navigator.userAgentData``, so a site sees the UA header claim Chrome while
the high-entropy hints say headless. Sites key off these to serve bot pages, blocks, or
stripped-down content, so the agent would not see what a person browsing manually sees.

This applies the well-known set of evasions so an ordinary site returns its real content:
a realistic Chrome user-agent matched to the actual engine version, a normal
locale/timezone/viewport, an init script (run before any page script, in every context)
that masks the JS-surface fingerprint, and a CDP user-agent override
(:func:`user_agent_override`, applied per page in ``browser.py``) that brings the client
hints + ``navigator.userAgentData`` into agreement with the UA string and strips the
``HeadlessChrome`` brand. Everything is pinned to one coherent identity — desktop Linux
Chrome — so UA string, client hints, ``navigator.platform`` and the WebGL renderer all
agree (the previous spoof claimed Linux in the UA but a macOS ``Intel Iris`` renderer).

This is **fingerprint normalization, not anti-bot evasion**: it gets ordinary sites to
return their real content. It deliberately does NOT defeat residential-proxy / CAPTCHA bot
walls (Cloudflare/DataDome class) — that needs a proxy network we don't run.
"""

from __future__ import annotations

from typing import TypedDict

# Runs before any page script in every context, masking the standard automation tells.
# The UA string, client hints and navigator.platform are handled natively by the CDP
# override (user_agent_override); this covers the rest of the JS surface.
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
// A faithful plugin list: real Chrome exposes five entries all aliasing the built-in PDF
// viewer. The old spoof returned [1,2,3,4,5], so plugins[0].name was undefined — itself a
// tell. This gives named entries that survive a closer look.
const _plugins = [
  ['PDF Viewer', 'internal-pdf-viewer'],
  ['Chrome PDF Viewer', 'internal-pdf-viewer'],
  ['Chromium PDF Viewer', 'internal-pdf-viewer'],
  ['Microsoft Edge PDF Viewer', 'internal-pdf-viewer'],
  ['WebKit built-in PDF', 'internal-pdf-viewer'],
].map(([name, filename]) => ({ name, filename, description: 'Portable Document Format',
  length: 1 }));
_plugins.item = (i) => _plugins[i] || null;
_plugins.namedItem = (n) => _plugins.find((p) => p.name === n) || null;
Object.defineProperty(navigator, 'plugins', { get: () => _plugins });
// A fuller window.chrome than the bare { runtime } stub — real Chrome carries app/csi/
// loadTimes, which a fingerprinter probes for.
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = window.chrome.app || {
  isInstalled: false,
  InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
  RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
};
window.chrome.csi = window.chrome.csi || function () { return {}; };
window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
const _query = window.navigator.permissions && window.navigator.permissions.query;
if (_query) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _query(p);
}
// A coherent Linux desktop GPU (the real renderer is SwiftShader/llvmpipe, which screams
// headless/VM). Patch both WebGL 1 and 2; vendor/renderer must match the Linux UA.
const _patchWebGL = (proto) => {
  if (!proto) return;
  const _getParameter = proto.getParameter;
  proto.getParameter = function (p) {
    if (p === 37445) return 'Google Inc. (Intel)';                    // UNMASKED_VENDOR_WEBGL
    if (p === 37446)                                                  // UNMASKED_RENDERER_WEBGL
      return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), ' +
        'OpenGL 4.6 (Core Profile) Mesa 22.0.5)';
    return _getParameter.call(this, p);
  };
};
_patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
_patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
"""

# Browser-level flags appended to the headless shell's launch (it forwards extra args to
# Chrome). These mask tells at the source — cleaner than patching them in page JS:
#   - AutomationControlled off: Chrome stops advertising the automation switch (and stops
#     setting navigator.webdriver), the surest fix for the webdriver tell.
#   - a normal locale + window size so the rendered page matches a real desktop.
# The last two close SSRF egress paths that the CONNECT proxy can't see — they keep the
# proxy the *only* way out, so a page can't reach a private host around it:
#   - --disable-quic: QUIC/HTTP3 is UDP straight to the origin, not via an HTTP proxy;
#     disabling it forces TCP through the proxy.
#   - WebRTC disable_non_proxied_udp: stops RTCPeerConnection sending STUN/UDP directly to
#     an attacker-chosen (possibly private) host, which would bypass the proxy entirely.
# NOTE: this does NOT switch the headless *mode*. The image ships the old `headless_shell`
# build, whose one virtue is a socat forwarder that exposes CDP off-loopback; modern Chrome
# refuses to do that, so a true `--headless=new` swap needs a custom image or browserless.
LAUNCH_FLAGS = [
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--window-size=1280,800",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]

_DEFAULT_MAJOR = "120"


def _major(browser_version: str | None) -> str:
    """The engine's major version (e.g. ``"120"``), so the UA string and the client-hint
    brand list agree with the real Chromium build. Defaults when unknown."""
    if browser_version:
        head = browser_version.split(".", 1)[0]
        if head.isdigit():
            return head
    return _DEFAULT_MAJOR


def realistic_user_agent(browser_version: str | None) -> str:
    """A normal desktop Chrome UA, matched to the engine's actual major version so the UA
    and feature set agree. The VM runs Linux Chromium, so the UA claims Linux —
    internally consistent (no Mac/Linux platform mismatch for a site to notice)."""
    return (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_major(browser_version)}.0.0.0 Safari/537.36"
    )


def user_agent_override(
    *, user_agent: str, locale: str, browser_version: str | None
) -> dict[str, object]:
    """Params for ``Emulation.setUserAgentOverride`` (applied per page over CDP).

    Overriding ``user_agent`` at the Playwright context level fixes the UA *string* but
    leaves the client hints untouched, so the headless shell keeps advertising a
    ``Sec-CH-UA`` brand of ``HeadlessChrome`` (on the header and via
    ``navigator.userAgentData``) — the contradiction that gets a stealthed UA blocked
    anyway. Supplying ``userAgentMetadata`` makes Chromium itself emit *every* client hint
    (low- and high-entropy) and populate ``navigator.userAgentData`` consistent with the
    UA, with the ``HeadlessChrome`` brand replaced by ``Google Chrome``. ``platform`` sets
    ``navigator.platform`` to match. Pinned to the same Linux identity as the UA string."""
    major = _major(browser_version)
    full = f"{major}.0.0.0"
    # GREASE: the placeholder brand is randomized by real Chrome each version, so sites
    # don't validate it — only that the genuine brands are present and HeadlessChrome isn't.
    brands = [
        {"brand": "Not_A Brand", "version": "8"},
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
    ]
    full_version_list = [
        {"brand": "Not_A Brand", "version": "8.0.0.0"},
        {"brand": "Chromium", "version": full},
        {"brand": "Google Chrome", "version": full},
    ]
    return {
        "userAgent": user_agent,
        "acceptLanguage": f"{locale},en;q=0.9",
        "platform": "Linux x86_64",
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": full_version_list,
            "fullVersion": full,
            "platform": "Linux",
            "platformVersion": "",  # Chrome reports no platform version on Linux
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        },
    }


class _Viewport(TypedDict):
    width: int
    height: int


class ContextOptions(TypedDict):
    user_agent: str
    locale: str
    timezone_id: str
    viewport: _Viewport
    device_scale_factor: int
    extra_http_headers: dict[str, str]


def context_options(*, user_agent: str, locale: str, timezone_id: str) -> ContextOptions:
    """Stealth-friendly ``new_context`` kwargs: a realistic UA, locale/timezone, a normal
    desktop viewport, and an Accept-Language header consistent with the locale. The client
    hints + ``navigator.userAgentData`` are brought into agreement separately, per page,
    by :func:`user_agent_override`."""
    return {
        "user_agent": user_agent,
        "locale": locale,
        "timezone_id": timezone_id,
        "viewport": {"width": 1280, "height": 800},
        "device_scale_factor": 1,
        "extra_http_headers": {"Accept-Language": f"{locale},en;q=0.9"},
    }
