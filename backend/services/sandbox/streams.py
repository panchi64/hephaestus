"""A host command's output as temp files, and the watcher that streams it while it runs.

Files rather than pipes: a pipe has to be drained while the process runs or it fills and
deadlocks, and the same files serve a background command's `check` without a second
mechanism for it.

**Nothing here may end a command.** The progress hook belongs to a caller and the files
belong to a process; a failure in either is logged and the loop carries on, because a
command that ran perfectly well must not be reported as failed by the thing that was only
watching it.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import IO, ClassVar

from .limits import PROGRESS_MAX_CHARS
from .shell_result import ProgressHook

logger = logging.getLogger(__name__)

#: How often a running command's new output is handed to the progress hook. Half a second
#: is under the threshold at which a build stops reading as live, and well above the rate
#: at which a chatty command would turn one tool call into thousands of stream frames.
_PROGRESS_INTERVAL_S = 0.5


def stream_files(prefix: str) -> tuple[IO[bytes], IO[bytes]]:
    """Two temp files for one command's stdout and stderr."""
    return (
        tempfile.NamedTemporaryFile(mode="w+b", prefix=f"hephaestus_{prefix}_out_", delete=False),
        tempfile.NamedTemporaryFile(mode="w+b", prefix=f"hephaestus_{prefix}_err_", delete=False),
    )


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def unlink(*paths: str) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


class _StreamTail:
    """One output file, read forward from wherever the last read stopped.

    Decoding is **incremental** rather than a decode per chunk: a read lands wherever the
    command happened to have written to, which is regularly the middle of a multi-byte
    character, and decoding each chunk on its own would turn every one of those into two
    replacement characters in the operator's terminal. The decoder holds the remainder
    until the rest of it arrives.
    """

    __slots__: ClassVar[tuple[str, ...]] = ("_path", "_offset", "_decoder")

    def __init__(self, path: str) -> None:
        self._path: str = path
        self._offset: int = 0
        self._decoder: codecs.IncrementalDecoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )

    def read(self) -> str:
        """Whatever has been written since the last call, or "" — including on an error.

        A file that cannot be read is a command whose output the operator will still see
        in full when it finishes; it is never a reason to interrupt one that is running.
        """
        try:
            with open(self._path, "rb") as handle:
                _ = handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return ""
        return self._decoder.decode(chunk) if chunk else ""


class Tail:
    """Both of a command's streams, polled while it runs and handed to a progress hook.

    It stops on a signal rather than on a cancellation, so the caller gets one final read
    after the process is gone — the output of a command's last half-second is exactly the
    part that says why it ended.
    """

    __slots__: ClassVar[tuple[str, ...]] = (
        "_out",
        "_err",
        "_started",
        "_hook",
        "_stop",
        "_streamed",
    )

    def __init__(self, out_path: str, err_path: str, *, started: float, hook: ProgressHook):
        self._out: _StreamTail = _StreamTail(out_path)
        self._err: _StreamTail = _StreamTail(err_path)
        self._started: float = started
        self._hook: ProgressHook = hook
        self._stop: asyncio.Event = asyncio.Event()
        self._streamed: int = 0

    def stop(self) -> None:
        """Ask the pump to take one last read and return."""
        self._stop.set()

    async def pump(self) -> None:
        while True:
            try:
                _ = await asyncio.wait_for(self._stop.wait(), _PROGRESS_INTERVAL_S)
            except TimeoutError:
                await self._tick()
                continue
            await self._tick()
            return

    async def _tick(self) -> None:
        remaining = PROGRESS_MAX_CHARS - self._streamed
        if remaining <= 0:
            return
        try:
            chunk = self._out.read() + self._err.read()
            if not chunk:
                return
            # The budget bounds the *tick* as well as the total: a command that printed a
            # megabyte between two reads would otherwise put all of it on the stream in
            # one frame, having never exceeded the budget on any earlier one.
            self._streamed += len(chunk)
            await self._hook(chunk[:remaining], time.monotonic() - self._started)
        except asyncio.CancelledError:
            raise
        except Exception:  # watching a command must not be able to fail it
            logger.debug("shell progress hook failed", exc_info=True)
