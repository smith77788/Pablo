"""Public facade of the library.

:class:`SpintaxEngine` wires the pipeline together (lexer -> parser -> semantic
analyzer -> validator -> generator -> formatter -> reporter) and adds an LRU
cache of compiled templates.  Everything the engine needs is injected through
the constructor — there is no global state.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from .analyzer import Analyzer, TemplateAnalysis
from .compiled import CompiledTemplate
from .context import Context
from .errors import ValidationFailedError, ValidationResult
from .formatter import Formatter
from .functions import FunctionHandler, FunctionRegistry
from .limits import DEFAULT_LIMITS, Limits
from .nodes import RootNode
from .parser import parse as _parse
from .reporter import ErrorReporter
from .validator import Validator

_DEFAULT_CACHE_SIZE = 128


class SpintaxEngine:
    """Thread-safe entry point; create one per configuration and reuse it."""

    def __init__(
        self,
        *,
        limits: Limits | None = None,
        functions: FunctionRegistry | None = None,
        lenient: bool = False,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        self._limits = limits or DEFAULT_LIMITS
        self._registry = functions or FunctionRegistry()
        self._lenient = lenient
        self._validator = Validator(self._limits, self._registry, lenient)
        self._formatter = Formatter()
        self._reporter = ErrorReporter()
        self._cache: OrderedDict[str, CompiledTemplate] = OrderedDict()
        self._cache_size = max(cache_size, 0)
        self._cache_lock = Lock()

    # --- pipeline stages ------------------------------------------------------

    def parse(self, template: str) -> RootNode:
        """Parse to an AST; raises :class:`TemplateSyntaxError` on bad input."""
        return _parse(template, self._limits, self._lenient)

    def validate(self, template: str | RootNode) -> ValidationResult:
        """Full syntactic + semantic check; never raises."""
        if isinstance(template, RootNode):
            return self._validator.validate_ast(template)
        return self._validator.validate_source(template)

    def compile(self, template: str) -> CompiledTemplate:
        """Parse, validate and cache; raises :class:`ValidationFailedError`."""
        cached = self._cache_get(template)
        if cached is not None:
            return cached
        root = self.parse(template)
        info = self._validator.analyze_ast(root)
        if info.has_errors:
            errors = [d for d in info.diagnostics if d.severity.value == "error"]
            raise ValidationFailedError(errors, template)
        compiled = CompiledTemplate(
            template, root, info.macros, self._limits, self._registry
        )
        self._cache_put(template, compiled)
        return compiled

    def generate(
        self,
        template: str,
        context: Context | None = None,
        *,
        seed: int | str | bytes | None = None,
    ) -> str:
        """Compile (cached) and generate one result."""
        return self.compile(template).generate(context, seed=seed)

    def generate_many(
        self,
        template: str,
        count: int,
        context: Context | None = None,
        *,
        seed: int | str | bytes | None = None,
        unique: bool = False,
    ) -> list[str]:
        """Compile (cached) and generate a batch."""
        return self.compile(template).generate_many(
            count, context, seed=seed, unique=unique
        )

    def analyze(self, template: str) -> TemplateAnalysis:
        """Static statistics: node count, depth, combinations, names used."""
        compiled = self.compile(template)
        info = self._validator.analyze_ast(compiled.root)
        return Analyzer(info.macros).analyze(
            compiled.root, info.variables, info.external_variables, info.functions
        )

    def format(self, template: str | RootNode) -> str:
        """Render a template (or AST) in canonical form."""
        root = template if isinstance(template, RootNode) else self.parse(template)
        return self._formatter.format(root)

    def explain(self, result: ValidationResult, template: str | None = None) -> str:
        """Human-readable rendering of validation diagnostics."""
        return self._reporter.format_many(result.diagnostics, template)

    # --- extension points --------------------------------------------------------

    def register_function(
        self,
        name: str,
        handler: FunctionHandler,
        *,
        min_args: int = 0,
        max_args: int | None = None,
        replace: bool = False,
    ) -> None:
        """Register a custom ``[name:...]`` function on this engine."""
        self._registry.register(
            name, handler, min_args=min_args, max_args=max_args, replace=replace
        )

    @property
    def functions(self) -> FunctionRegistry:
        return self._registry

    @property
    def limits(self) -> Limits:
        return self._limits

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # --- cache ---------------------------------------------------------------------

    def _cache_get(self, template: str) -> CompiledTemplate | None:
        if not self._cache_size:
            return None
        with self._cache_lock:
            compiled = self._cache.get(template)
            if compiled is not None:
                self._cache.move_to_end(template)
            return compiled

    def _cache_put(self, template: str, compiled: CompiledTemplate) -> None:
        if not self._cache_size:
            return
        with self._cache_lock:
            self._cache[template] = compiled
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
