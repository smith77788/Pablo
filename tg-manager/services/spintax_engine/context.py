"""Generation context: per-call inputs to the generator.

A :class:`Context` is immutable, so one instance can safely be shared between
batch items and threads.  Everything that can vary between two calls to
``generate()`` — variables, seed, locale, timezone, clock, extra functions,
missing-variable policy, output formats — lives here, never in module state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, tzinfo
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from .functions import FunctionRegistry

MissingVariablePolicy = Literal["error", "empty", "keep"]


@dataclass(frozen=True, slots=True)
class Context:
    """Inputs for one generation (or one batch of generations)."""

    variables: Mapping[str, object] = field(default_factory=dict)
    """Values for ``{{external}}`` placeholders."""

    seed: int | str | bytes | None = None
    """Deterministic seed; identical seed + template => identical output."""

    locale: str | None = None
    """BCP-47 tag, available to custom functions via ``env.context.locale``."""

    timezone: str | tzinfo | None = None
    """Timezone for ``[date]``/``[time]``/... — an IANA name or a tzinfo."""

    now: datetime | None = None
    """Clock override; when set, all date/time constructs use this instant."""

    functions: FunctionRegistry | None = None
    """Per-call function overrides layered over the engine registry."""

    on_missing_variable: MissingVariablePolicy = "error"
    """What to do when ``$name`` or ``{{name}}`` has no value:
    ``error`` raises, ``empty`` emits nothing, ``keep`` emits the placeholder."""

    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M:%S"
    datetime_format: str = "%Y-%m-%d %H:%M:%S"

    def resolve_timezone(self) -> tzinfo | None:
        if isinstance(self.timezone, str):
            return ZoneInfo(self.timezone)
        return self.timezone

    def current_datetime(self) -> datetime:
        if self.now is not None:
            return self.now
        return datetime.now(self.resolve_timezone())

    def with_seed(self, seed: int | str | bytes | None) -> "Context":
        return replace(self, seed=seed)


EMPTY_CONTEXT = Context()
