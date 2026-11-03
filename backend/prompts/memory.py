"""The memory catalog (`MEM-4`), surfaced as a dynamic instruction.

Pinned memories are listed as ``- <id>: <description>`` lines, sorted by id so the block
keeps its bytes until the pinned set changes; the rest are a count and a pointer to search.
"""

from __future__ import annotations

# ``{entries}`` is the pinned lines; ``{others}`` is empty or :data:`MEMORY_OTHERS`.
MEMORY_CATALOG = """\
Pinned memories are what the operator wants you to keep in mind in every conversation: \
how to address them, standing preferences, facts about their setup. Follow them from \
your first reply. Each line is an index entry; when a task depends on the details, call \
`memory_open` with its id.

{entries}{others}"""

# ``{count}`` is "N more memories".
MEMORY_OTHERS = """

Long-term memory holds {count}. When the conversation touches something the operator may \
have told you before, search them with `memory_recall` before asking again."""

# ``{entries}`` is ``- <description>: <content>`` lines, best match first.
MEMORY_RECALLED = """\
Memories matching the operator's message, from `memory_recall` on it just now:

{entries}"""

# How many recalled memories a turn's prompt carries.
MEMORY_RECALLED_LIMIT = 3

# The cosine similarity a memory needs to ride a turn's prompt. Every memory with an
# embedding gets a dense score and any shared word ("my") gets a keyword one, so neither
# rank nor `matched_by` separates a real match. A starting point for common embedding
# models, not measured against the operator's.
MEMORY_RECALLED_SIMILARITY = 0.6

# How many pinned memories the catalog lists; the oldest pins past it are counted instead.
MEMORY_PINNED_LIMIT = 20
