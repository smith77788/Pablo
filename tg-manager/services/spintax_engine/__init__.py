"""SpintaxAI — production-grade text generation engine.

Quick start::

    import spintaxai

    spintaxai.generate("{Hello|Hi} world")          # one result
    spintaxai.generate_many("{A|B|C}", 10)          # a batch
    spintaxai.validate("{oops")                     # diagnostics, never raises

For custom limits or functions create your own engine::

    engine = spintaxai.SpintaxEngine(limits=spintaxai.Limits(max_batch_size=10))
    engine.register_function("upper", lambda env, args: args[0].upper(),
                             min_args=1, max_args=1)
"""

from __future__ import annotations

from .analyzer import TemplateAnalysis
from .compiled import CompiledTemplate
from .context import Context
from .engine import SpintaxEngine
from .errors import (
    Diagnostic,
    ErrorCode,
    GenerationError,
    LimitExceededError,
    SemanticError,
    Severity,
    SourceSpan,
    SpintaxError,
    TemplateSyntaxError,
    ValidationFailedError,
    ValidationResult,
)
from .functions import (
    FunctionEnv,
    FunctionHandler,
    FunctionRegistry,
    FunctionSpec,
    register_text_functions,
)
from .limits import Limits
from .nodes import RootNode
from .reporter import ErrorReporter

__version__ = "0.1.0"

__all__ = [
    "SpintaxEngine",
    "CompiledTemplate",
    "Context",
    "Limits",
    "FunctionRegistry",
    "FunctionSpec",
    "FunctionEnv",
    "FunctionHandler",
    "register_text_functions",
    "TemplateAnalysis",
    "ValidationResult",
    "Diagnostic",
    "Severity",
    "SourceSpan",
    "ErrorCode",
    "ErrorReporter",
    "SpintaxError",
    "TemplateSyntaxError",
    "SemanticError",
    "GenerationError",
    "LimitExceededError",
    "ValidationFailedError",
    "RootNode",
    "parse",
    "validate",
    "compile",
    "generate",
    "generate_many",
    "analyze",
    "format",
    "default_engine",
]

#: Shared engine behind the module-level convenience functions.
default_engine = SpintaxEngine()

parse = default_engine.parse
validate = default_engine.validate
compile = default_engine.compile  # noqa: A001 - deliberate, mirrors the public API spec
generate = default_engine.generate
generate_many = default_engine.generate_many
analyze = default_engine.analyze
format = default_engine.format  # noqa: A001 - deliberate, mirrors the public API spec
