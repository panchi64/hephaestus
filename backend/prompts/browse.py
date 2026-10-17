"""What is true about *this* browser and not about a browser in general.

`pydantic_ai_harness` already writes the generic half — which tool reads a page, how to
get an `aria-ref` handle, what to do about iframes and spinners and infinite lists — and
that text is maintained upstream against the tools it describes, so it is borrowed rather
than restated (``tools/browse.browse_instructions``). Restating it here would mean two
descriptions of eighteen tools drifting apart, and the one that drifted would still be
shipped.

What the harness cannot know is the three things below: the operator watches and drives
the same browser, the session outlives the turn, and either of those can end it. Those are
this installation's, so they live here.

Kept short on purpose. This rides at the **head** of every request for as long as a thread
has a browser open, where it is byte-stable and cache-friendly but never free.
"""

from __future__ import annotations

#: Appended to the harness's own browser instructions. Second person, imperative, and
#: about *this* browser only.
BROWSER_ADDENDUM = """
The operator watches this browser live in the app and can drive it too: click something
themselves, or log into a site so that you do not have to hold their password. Logins are
one profile shared by every conversation, so a site they logged into earlier is usually
still signed in. Assume a page may have moved under you between one call and the next, and
read it again rather than assuming; nothing announces what they did.

The browser stays open across turns and its tabs belong to this conversation, so the page
you left is the page you come back to. Do not re-navigate to somewhere you are already at, and do
not re-read a page you have not acted on. `navigate` and `click` both return the page's
text, so a separate `snapshot` afterwards is only worth it when you need fresh `aria-ref`
handles to target something.

Use more than one tab. `tabs('new')` opens a blank one and makes it active, so you can keep
search results, a reference page or a form you are filling in one tab and work in another,
then `tabs('list')` and `tabs('select', index)` to move between them — far better than
navigating the same tab back and forth and losing your place each time. Comparing several
sources, or following a link without abandoning the page that named it, is what tabs are
for. Every other tool acts on the active tab only.

A screenshot's result gives its id. When your answer depends on something the operator
should see, embed that screenshot in the answer as `![what it shows](screenshot:<id>)`, and
only one you took in this conversation.

The operator can also end the browser whenever they like. If a tool tells you they did,
that is a decision and not an error to retry: the page and anything unsaved on it are gone,
though the logins are kept. Say where you had got to, and either start again with
`navigate` if the work still needs a browser, or finish with `web_fetch` / `web_search`,
or ask them how they want to go on.

"Allowed domains: all" above means no site allowlist is applied — but the browser runs in
an isolated VM that reaches public addresses only, so `localhost`, the operator's machine
and internal hosts are not reachable from here.
""".strip()
