"""Agent names as traced. Every other name is background work on the utility model."""

MAIN_AGENT = "main"
SUBAGENT_AGENT = "subagent"
TURN_AGENTS = frozenset({MAIN_AGENT, SUBAGENT_AGENT})
