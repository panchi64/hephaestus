"""Numbers the sandbox and its callers must agree on."""

from __future__ import annotations

#: The exit a timeout reports, from the guest's ``timeout`` and from a host command killed on
#: its wall clock alike: the command was stopped, not failed.
TIMEOUT_EXIT = 124

#: How long a first command waits for a VM that is still booting.
VM_WAIT_S = 120.0

#: How much of one command's output is streamed while it runs. The whole of it still comes
#: back on the result; this bounds what the live stream and its replay buffer carry for a
#: command that prints megabytes.
PROGRESS_MAX_CHARS = 200_000
