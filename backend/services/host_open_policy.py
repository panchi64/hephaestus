"""Which files the operator's machine may be asked to open — the *type* half of the fence.

``services/host_open`` decides WHERE a path may point (inside the operator's own project
roots) and then hands it to the desktop's "open this with whatever handles it" program.
Location alone is not a gate: the agent *writes into* those roots, so a path can be
perfectly contained and still be a program. ``build-results.command`` on macOS,
``report.desktop`` on Linux, ``report.bat`` on Windows — each is a file an ordinary
workspace-write produces, which the model can then name in its own prose behind whatever
anchor text it likes ("click to view the results"), and which the host **executes** the
moment the operator clicks. Nothing else stands on that path: it is operator-initiated,
so no approval gate sees it.

So the type is fenced here, **deny-by-default**, because the feature's purpose is narrow —
the operator asked for the files being read to open in their editor. That is an allowlist
of text, source and documents, and anything it doesn't recognise costs a sentence rather
than a subprocess. A denylist of executable suffixes would be the wrong shape: it is
wrong the day a host registers a new one, and it is wrong *now* for the spellings a shell
normalises away (``report.md.``, ``report.md␠``, which resolve back to a neighbouring
file on Windows). An allowlist has no such holes — it matches exactly, or it refuses.

Two things a suffix cannot say, checked alongside it:

* **the execute bit** — ``notes.py`` with ``+x`` is a script the shell runs, and its
  extension is silent about that;
* **what the name is hung on** — a macOS application bundle (``Foo.app``,
  ``Foo.workflow``) is a *directory*, so a folder opens only when it carries no
  extension at all.

Deliberately not shared with ``corpus/folder``'s text-extension set, which looks like the
same list and must not become it: a crawler *should* index ``deploy.sh``, and this must
never hand one to a shell.
"""

from __future__ import annotations

import platform
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISREG

from core.exceptions import PermissionDeniedError

#: Everything the operator may open from an answer. Grouped by why each group is here,
#: because the reason is the maintenance rule: a suffix earns its place by being read by
#: a viewer, never by being run by a handler. Shell and scripting-host types
#: (``.sh``, ``.command``, ``.ps1``, ``.vbs``, ``.desktop``, ``.scpt``) are absent on
#: purpose — running the file *is* what their registered handler does. So are the
#: macro-bearing Office formats (``.docm``, ``.xlsm``) and the pointer files
#: (``.lnk``, ``.url``, ``.webloc``), which are an indirection to another program.
_OPENABLE: frozenset[str] = frozenset(
    {
        # Prose, notes, and the plain-text spill of a run.
        ".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".log", ".adoc", ".org",
        # Data and configuration the operator reads as text.
        ".json", ".jsonl", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".properties", ".env", ".csv", ".tsv", ".xml", ".plist", ".lock", ".diff",
        ".patch", ".gitignore", ".gitattributes", ".gitmodules", ".dockerignore",
        ".editorconfig", ".npmrc", ".nvmrc", ".prettierrc", ".eslintrc",
        # Source. The feature exists for these: a file a worktree thread just wrote, opened
        # in whatever the operator has registered for it.
        ".py", ".pyi", ".ipynb", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts",
        ".cts", ".go", ".rs", ".rb", ".php", ".java", ".kt", ".kts", ".swift", ".m",
        ".mm", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".scala", ".clj", ".ex",
        ".exs", ".erl", ".hs", ".lua", ".pl", ".r", ".jl", ".dart", ".vue", ".svelte",
        ".sql", ".graphql", ".gql", ".proto", ".tf", ".tfvars", ".css", ".scss",
        ".sass", ".less", ".styl",
        # Documents a viewer renders rather than executes.
        ".pdf", ".rtf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub",
        # Images, including the screenshots a tool leaves in the work log.
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic",
        ".ico",
        # Pages a browser renders inside its own sandbox — the report an agent writes
        # up. Script in one runs where every web page's script already runs, which is a
        # different thing from running on the host.
        ".html", ".htm", ".svg",
    }
)

#: Suffixes one host runs even though they are ordinary text on every other. ``.js`` is
#: the case that matters: Windows hands a double-clicked ``.js`` to Windows Script Host,
#: which executes it, while everywhere else it is a source file the operator wants in
#: their editor. Same file, two meanings — so the answer has to know which host it is on.
_HOST_EXECUTES: dict[str, frozenset[str]] = {"Windows": frozenset({".js"})}


def openable_suffixes() -> frozenset[str]:
    """The allowlist as it applies *here*, minus whatever this host executes."""
    return _OPENABLE - _HOST_EXECUTES.get(platform.system(), frozenset())


def _extension(name: str) -> str:
    """The type marker a handler dispatches on: everything from the last dot, lowercased.

    Not ``Path.suffix``, which reports *nothing* for a leading-dot name — it reads
    ``.gitignore`` and ``.command`` alike as a stem with no extension, and the second is
    a file whose entire name is a type. Taken this way both are ordinary entries in the
    allowlist above, and neither slips through the extension-less door that ``Makefile``
    and ``LICENSE`` come in by.
    """
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def ensure_openable(target: Path) -> None:
    """Refuse ``target`` unless it is something to *read* rather than something to run.

    Raises :class:`PermissionDeniedError` — a 403, the same answer as a path outside the
    projects, because it is the same fence seen from the other side. The message names
    the file and the reason: a refusal the operator can't act on reads as a broken
    control, and they are entitled to open the thing themselves if they meant to.

    One ``stat`` answers all three questions, so nothing can change between them.
    """
    name = target.name
    try:
        mode = target.stat().st_mode
    except OSError as exc:
        raise PermissionDeniedError(
            f"couldn't tell what kind of file {name} is, so it wasn't opened"
        ) from exc

    if S_ISDIR(mode):
        # ``Path.suffix`` here, not ``_extension``: a leading-dot folder is the hidden
        # directory every repo carries (``.github``, ``.venv``), while a bundle the host
        # would launch always has a stem in front of its extension.
        if target.suffix:
            raise PermissionDeniedError(
                f"{name} is a folder with an extension, which your machine may launch as "
                "an application rather than show, so it isn't opened from here"
            )
        return

    if not S_ISREG(mode):
        raise PermissionDeniedError(f"{name} isn't a regular file, so there's nothing to open")

    extension = _extension(name)
    if extension and extension not in openable_suffixes():
        raise PermissionDeniedError(
            f"{name} isn't a kind of file this opens — only text, source and documents, "
            "because your machine would run some of the rest rather than show them"
        )
    if S_IMODE(mode) & 0o111:
        raise PermissionDeniedError(
            f"{name} is marked executable, so opening it could run it instead of showing "
            "it; clear the execute bit if it's meant to be read"
        )
