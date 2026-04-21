from importlib.metadata import version
from typing import Final

__all__: Final[list[str]] = ["__version__"]

__version__: str = version("cli-tools")
