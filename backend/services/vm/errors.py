"""What the VM layer raises."""

from core.exceptions import HephaestusError


class VMError(HephaestusError):
    """A machine failed: it would not boot, stopped answering, or a call into it broke.
    A command's non-zero exit is a result, not this. The message is for the operator."""


class MachineUnavailableError(VMError):
    """No machine can run here now."""


class ImageBuildError(VMError):
    """The guest image could not be built."""
