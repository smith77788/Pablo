"""CompiledTemplate: a parsed, semantically checked, reusable template.

Compiling once and generating many times skips all lexing/parsing/validation
work on the hot path.  Instances are immutable and thread-safe: every
``generate`` call gets its own RNG and run state.
"""

from __future__ import annotations

import random

from .context import EMPTY_CONTEXT, Context
from .errors import Diagnostic, ErrorCode, GenerationError, SourceSpan
from .functions import FunctionRegistry
from .generator import Generator
from .limits import DEFAULT_LIMITS, Limits
from .nodes import MacroDefinitionNode, RootNode


class CompiledTemplate:
    """Produced by :meth:`SpintaxEngine.compile`; do not construct directly."""

    __slots__ = ("source", "root", "macros", "_limits", "_generator")

    def __init__(
        self,
        source: str,
        root: RootNode,
        macros: dict[str, MacroDefinitionNode],
        limits: Limits = DEFAULT_LIMITS,
        registry: FunctionRegistry | None = None,
    ) -> None:
        self.source = source
        self.root = root
        self.macros = macros
        self._limits = limits
        self._generator = Generator(root, macros, limits, registry, source)

    def generate(
        self,
        context: Context | None = None,
        *,
        seed: int | str | bytes | None = None,
    ) -> str:
        context = self._resolve_context(context, seed)
        return self._generator.generate(context)

    def generate_many(
        self,
        count: int,
        context: Context | None = None,
        *,
        seed: int | str | bytes | None = None,
        unique: bool = False,
    ) -> list[str]:
        context = self._resolve_context(context, seed)
        if count < 0:
            raise ValueError("count must be non-negative")
        if count > self._limits.max_batch_size:
            raise GenerationError(
                Diagnostic.error(
                    ErrorCode.BATCH_TOO_LARGE,
                    f"Requested {count} results; the limit is "
                    f"{self._limits.max_batch_size}",
                    SourceSpan.point(0, 1, 1),
                ),
                self.source,
            )
        master = (
            random.Random(context.seed) if context.seed is not None else random.Random()
        )
        if not unique:
            return [
                self._generator.generate(context, rng=random.Random(master.getrandbits(64)))
                for _ in range(count)
            ]
        return self._generate_unique(count, context, master)

    # --- helpers -------------------------------------------------------------

    def _resolve_context(
        self, context: Context | None, seed: int | str | bytes | None
    ) -> Context:
        context = context or EMPTY_CONTEXT
        if seed is not None:
            context = context.with_seed(seed)
        return context

    def _generate_unique(
        self, count: int, context: Context, master: random.Random
    ) -> list[str]:
        results: list[str] = []
        seen: set[str] = set()
        attempts = 0
        max_attempts = count * self._limits.max_unique_attempts_factor
        while len(results) < count:
            if attempts >= max_attempts:
                raise GenerationError(
                    Diagnostic.error(
                        ErrorCode.UNIQUE_POOL_EXHAUSTED,
                        f"Produced only {len(results)} unique results out of "
                        f"{count} after {attempts} attempts",
                        SourceSpan.point(0, 1, 1),
                    ),
                    self.source,
                )
            attempts += 1
            result = self._generator.generate(
                context, rng=random.Random(master.getrandbits(64))
            )
            if result not in seen:
                seen.add(result)
                results.append(result)
        return results
