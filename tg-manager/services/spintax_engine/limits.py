"""Configurable security limits.

Every hard cap the engine enforces lives here, so operators can tune the
engine for their workload without touching any other module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource limits enforced across the pipeline.

    The defaults are safe for a multi-tenant bot backend; raise them for
    trusted offline workloads.
    """

    max_input_length: int = 1_000_000
    """Maximum template length in characters (lexer)."""

    max_nesting_depth: int = 64
    """Maximum depth of nested constructs (parser)."""

    max_nodes: int = 200_000
    """Maximum number of AST nodes in one template (parser)."""

    max_macro_depth: int = 32
    """Maximum macro-inside-macro expansion depth (generator)."""

    max_output_length: int = 1_000_000
    """Maximum length of one generated result in characters (generator)."""

    max_generation_steps: int = 2_000_000
    """Upper bound on node evaluations per generation (generator)."""

    max_batch_size: int = 100_000
    """Maximum ``count`` accepted by ``generate_many`` (engine)."""

    max_unique_attempts_factor: int = 20
    """In unique batch mode, give up after ``count * factor`` attempts."""

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"Limits.{name} must be a positive integer, got {value!r}")


DEFAULT_LIMITS = Limits()
