"""Validator: orchestrates lexing, parsing and semantic analysis.

Unlike ``parse()`` (which raises on the first problem), ``validate()`` always
returns a :class:`ValidationResult` — syntax errors become diagnostics, and a
syntactically valid template additionally gets the full semantic check.
"""

from __future__ import annotations

from .errors import SpintaxError, ValidationResult
from .functions import FunctionRegistry
from .limits import DEFAULT_LIMITS, Limits
from .nodes import RootNode
from .parser import parse
from .semantics import SemanticAnalyzer, SemanticInfo


class Validator:
    def __init__(
        self,
        limits: Limits = DEFAULT_LIMITS,
        registry: FunctionRegistry | None = None,
        lenient: bool = False,
    ) -> None:
        self._limits = limits
        self._registry = registry
        self._lenient = lenient
        self._analyzer = SemanticAnalyzer(registry)

    def validate_source(self, source: str) -> ValidationResult:
        try:
            root = parse(source, self._limits, self._lenient)
        except SpintaxError as error:
            return ValidationResult(diagnostics=(error.diagnostic,))
        return self.validate_ast(root)

    def validate_ast(self, root: RootNode) -> ValidationResult:
        info = self._analyzer.analyze(root)
        return ValidationResult(diagnostics=info.diagnostics)

    def analyze_ast(self, root: RootNode) -> SemanticInfo:
        return self._analyzer.analyze(root)
