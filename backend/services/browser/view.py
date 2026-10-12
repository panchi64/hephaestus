"""The operator's live view of a conversation's browser: frames down, input up.

The browser runs headless in the platform VM, so the operator sees it as a CDP screencast
of the session's current page and drives it by sending mouse, wheel, keyboard and
navigation events that are replayed through CDP's Input domain. Chromium withholds the
next frame until the last one is acknowledged, and a watcher keeps only the newest frame,
so a slow client sees a lower frame rate rather than a backlog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from .live import LiveBrowser

logger = logging.getLogger(__name__)

#: How often the stream checks whether the session moved to another tab or ended.
_POLL_S = 0.5
_SCREENCAST = {"format": "jpeg", "quality": 70, "maxWidth": 1600, "maxHeight": 1200}
_NAVIGATION_TIMEOUT_MS = 30_000


class MouseInput(BaseModel):
    type: Literal["mouse"]
    action: Literal["down", "up", "move"]
    x: float
    y: float
    button: Literal["left", "middle", "right", "none"] = "none"
    click_count: int = Field(default=0, ge=0, le=3)
    modifiers: int = Field(default=0, ge=0, le=15)


class WheelInput(BaseModel):
    type: Literal["wheel"]
    x: float
    y: float
    delta_x: float = 0
    delta_y: float = 0
    modifiers: int = Field(default=0, ge=0, le=15)


class KeyInput(BaseModel):
    type: Literal["key"]
    action: Literal["down", "up"]
    key: str = Field(max_length=64)
    code: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=8)
    key_code: int = Field(default=0, ge=0, le=1024)
    modifiers: int = Field(default=0, ge=0, le=15)


class TextInput(BaseModel):
    type: Literal["text"]
    text: str = Field(max_length=100_000)


class NavigateInput(BaseModel):
    type: Literal["navigate"]
    url: str = Field(pattern=r"^https?://", max_length=8192)


class HistoryInput(BaseModel):
    type: Literal["history"]
    action: Literal["back", "forward", "reload"]


ViewInput = Annotated[
    MouseInput | WheelInput | KeyInput | TextInput | NavigateInput | HistoryInput,
    Field(discriminator="type"),
]

_MOUSE_TYPES = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}


async def frames(live: LiveBrowser, alive: Callable[[], bool]) -> AsyncIterator[dict[str, Any]]:
    """Screencast frames of the session's current page while ``alive()``, following the
    agent from tab to tab; a ``closed`` event when the session ends."""
    newest: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    acks: set[asyncio.Task[Any]] = set()
    page: Any = None
    cdp: Any = None
    title = ""

    def on_frame(session: Any, params: dict[str, Any]) -> None:
        # Acknowledged on the session it came from, which may still be starting.
        ack = asyncio.ensure_future(
            session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        )
        acks.add(ack)
        ack.add_done_callback(_settled(acks))
        meta = params.get("metadata", {})
        frame = {
            "type": "frame",
            "data": params["data"],
            "width": meta.get("deviceWidth", 0),
            "height": meta.get("deviceHeight", 0),
        }
        with contextlib.suppress(asyncio.QueueEmpty):
            newest.get_nowait()
        newest.put_nowait(frame)

    try:
        while alive():
            current = live.session.page
            if current is not page:
                await _stop(cdp)
                cdp, page = None, current
                if page is not None:
                    cdp = await _start(page, on_frame)
            try:
                frame = await asyncio.wait_for(newest.get(), _POLL_S)
            except TimeoutError:
                continue
            url = getattr(page, "url", "") or ""
            with contextlib.suppress(Exception):
                title = await page.title()
            live.touch()
            yield {**frame, "url": url, "title": title}
        yield {"type": "closed"}
    finally:
        await _stop(cdp)
        for ack in acks:
            ack.cancel()


async def _start(page: Any, on_frame: Callable[[Any, dict[str, Any]], None]) -> Any:
    try:
        cdp = await page.context.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda params: on_frame(cdp, params))
        await cdp.send("Page.startScreencast", _SCREENCAST)
    except Exception:
        logger.debug("browser view: could not start a screencast", exc_info=True)
        return None
    return cdp


async def _stop(cdp: Any) -> None:
    if cdp is None:
        return
    with contextlib.suppress(Exception):
        await cdp.send("Page.stopScreencast")
    with contextlib.suppress(Exception):
        await cdp.detach()


def _settled(tasks: set[asyncio.Task[Any]]) -> Callable[[asyncio.Task[Any]], None]:
    def done(task: asyncio.Task[Any]) -> None:
        tasks.discard(task)
        if not task.cancelled():
            task.exception()

    return done


async def dispatch(live: LiveBrowser, event: ViewInput) -> None:
    """Replay one operator input on the session's current page."""
    page = live.session.page
    if page is None:
        return
    live.touch()
    match event:
        case NavigateInput(url=url):
            await page.goto(url, wait_until="commit", timeout=_NAVIGATION_TIMEOUT_MS)
        case HistoryInput(action="back"):
            await page.go_back(wait_until="commit", timeout=_NAVIGATION_TIMEOUT_MS)
        case HistoryInput(action="forward"):
            await page.go_forward(wait_until="commit", timeout=_NAVIGATION_TIMEOUT_MS)
        case HistoryInput():
            await page.reload(wait_until="commit", timeout=_NAVIGATION_TIMEOUT_MS)
        case _:
            cdp = await _input_session(live, page)
            method, params = _input_command(event)
            await cdp.send(method, params)


def _input_command(event: ViewInput) -> tuple[str, dict[str, Any]]:
    match event:
        case MouseInput():
            return "Input.dispatchMouseEvent", {
                "type": _MOUSE_TYPES[event.action],
                "x": event.x,
                "y": event.y,
                "button": event.button,
                "clickCount": event.click_count,
                "modifiers": event.modifiers,
            }
        case WheelInput():
            return "Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": event.x,
                "y": event.y,
                "deltaX": event.delta_x,
                "deltaY": event.delta_y,
                "modifiers": event.modifiers,
            }
        case KeyInput():
            kind = "keyUp" if event.action == "up" else ("keyDown" if event.text else "rawKeyDown")
            params: dict[str, Any] = {
                "type": kind,
                "key": event.key,
                "code": event.code,
                "windowsVirtualKeyCode": event.key_code,
                "nativeVirtualKeyCode": event.key_code,
                "modifiers": event.modifiers,
            }
            if kind == "keyDown":
                params["text"] = event.text
                params["unmodifiedText"] = event.text
            return "Input.dispatchKeyEvent", params
        case TextInput():
            return "Input.insertText", {"text": event.text}
    raise ValueError(f"not a CDP input event: {event.type}")


async def _input_session(live: LiveBrowser, page: Any) -> Any:
    """One CDP session per page for input, kept while the session stays on that page."""
    held = live.input_channel
    if held is not None and held[0] is page:
        return held[1]
    cdp = await page.context.new_cdp_session(page)
    live.input_channel = (page, cdp)
    return cdp
