"""Public exports for the Maxwell 2D PyAEDT helper library."""

from .core import (
    BuildResult,
    CoilSpec,
    Maxwell2DToolkit,
    Maxwell2DToolkitError,
    WindingResult,
    build_from_spec,
    check_pyaedt_version,
    launch_maxwell2d,
    set_global_variables_low_level,
    set_local_variables_low_level,
)

__all__ = [
    "BuildResult",
    "CoilSpec",
    "Maxwell2DToolkit",
    "Maxwell2DToolkitError",
    "WindingResult",
    "build_from_spec",
    "check_pyaedt_version",
    "launch_maxwell2d",
    "set_global_variables_low_level",
    "set_local_variables_low_level",
]
