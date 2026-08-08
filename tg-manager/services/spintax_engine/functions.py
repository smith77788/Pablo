"""User-extensible function registry.

Custom ``[name:arg]`` constructs are plain Python callables registered here —
the core never needs to change.  Built-in constructs (``[uuid]``, ``[rand]``,
``[date]``, ...) have dedicated AST nodes and their names are reserved.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from .context import Context

#: Names handled by dedicated AST nodes; they cannot be overridden.
RESERVED_FUNCTION_NAMES = frozenset(
    {"uuid", "timestamp", "unix", "time", "datetime", "rand", "randf", "pick", "date"}
)


@dataclass(slots=True)
class FunctionEnv:
    """Everything a custom function may need at generation time.

    ``rng`` is the seeded generator — functions that use it stay deterministic
    under a fixed seed.  ``now`` is the single instant used for the whole
    generation, so all date/time output is mutually consistent.
    """

    rng: random.Random
    context: "Context"
    now: datetime


FunctionHandler = Callable[[FunctionEnv, Sequence[str]], str]


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    name: str
    handler: FunctionHandler = field(repr=False)
    min_args: int = 0
    max_args: int | None = None

    def accepts(self, count: int) -> bool:
        if count < self.min_args:
            return False
        return self.max_args is None or count <= self.max_args

    def arity_text(self) -> str:
        if self.max_args is None:
            return f"{self.min_args}+"
        if self.min_args == self.max_args:
            return str(self.min_args)
        return f"{self.min_args}..{self.max_args}"


class FunctionRegistry:
    """A mutable name -> :class:`FunctionSpec` mapping with override chaining."""

    def __init__(self, parent: "FunctionRegistry | None" = None) -> None:
        self._specs: dict[str, FunctionSpec] = {}
        self._parent = parent

    def register(
        self,
        name: str,
        handler: FunctionHandler,
        *,
        min_args: int = 0,
        max_args: int | None = None,
        replace: bool = False,
    ) -> None:
        if not name.isidentifier():
            raise ValueError(f"Function name {name!r} is not a valid identifier")
        if name in RESERVED_FUNCTION_NAMES:
            raise ValueError(f"Function name {name!r} is reserved by a built-in")
        if max_args is not None and max_args < min_args:
            raise ValueError("max_args must be >= min_args")
        if not replace and name in self._specs:
            raise ValueError(f"Function {name!r} is already registered")
        self._specs[name] = FunctionSpec(name, handler, min_args, max_args)

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)

    def get(self, name: str) -> FunctionSpec | None:
        spec = self._specs.get(name)
        if spec is None and self._parent is not None:
            return self._parent.get(name)
        return spec

    def names(self) -> frozenset[str]:
        own = frozenset(self._specs)
        return own | self._parent.names() if self._parent else own

    def child(self) -> "FunctionRegistry":
        """A registry layered on top of this one (per-call overrides)."""
        return FunctionRegistry(parent=self)


def register_text_functions(registry: FunctionRegistry) -> None:
    """Optional text helpers: ``[uppercase:x]``, ``[lowercase:x]``, ``[capitalize:x]``.

    Shipped as a demonstration of the extension mechanism — call this
    explicitly to enable them.
    """
    registry.register(
        "uppercase", lambda env, args: args[0].upper(), min_args=1, max_args=1
    )
    registry.register(
        "lowercase", lambda env, args: args[0].lower(), min_args=1, max_args=1
    )
    registry.register(
        "capitalize", lambda env, args: args[0].capitalize(), min_args=1, max_args=1
    )
