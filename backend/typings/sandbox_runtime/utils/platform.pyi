from typing import Literal

Platform = Literal["macos", "linux", "windows", "unknown"]

def get_platform() -> Platform: ...
