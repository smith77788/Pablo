"""Error model of the engine.

Every problem the engine can detect is described by an :class:`ErrorCode`
(a stable machine-readable identifier) and reported as a :class:`Diagnostic`
carrying the exact source location.  Exceptions raised by the public API wrap
diagnostics, so callers can render them with :class:`spintaxai.reporter.ErrorReporter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Half-open region ``[offset, offset + length)`` of the template source."""

    offset: int
    length: int
    line: int
    column: int

    @staticmethod
    def point(offset: int, line: int, column: int) -> "SourceSpan":
        return SourceSpan(offset=offset, length=1, line=line, column=column)


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@unique
class ErrorCode(Enum):
    """Stable error codes.  The numeric ranges group codes by pipeline stage:

    * ``E0xx`` — lexical, ``E1xx`` — syntax, ``E2xx`` — semantic,
    * ``E3xx`` — generation (runtime), ``E4xx`` — security limits,
    * ``W9xx`` — warnings.
    """

    # --- lexical -----------------------------------------------------------
    DANGLING_ESCAPE = "E001"

    # --- syntax ------------------------------------------------------------
    UNCLOSED_BRACE = "E101"
    UNCLOSED_BRACKET = "E102"
    UNEXPECTED_CLOSING = "E103"
    EMPTY_CHOICE = "E104"
    STRAY_PIPE = "E105"
    UNKNOWN_BRACKET = "E106"
    INVALID_WEIGHT = "E107"
    INVALID_PERCENT = "E108"
    BAD_FUNCTION_ARITY = "E109"
    PIPE_IN_ASSIGNMENT = "E110"
    EMPTY_MACRO_BODY = "E111"
    WEIGHT_IN_SHUFFLE = "E112"

    # --- semantic ----------------------------------------------------------
    UNDEFINED_MACRO = "E201"
    MACRO_CYCLE = "E202"
    MACRO_REDEFINED = "E203"
    UNKNOWN_FUNCTION = "E204"
    INVALID_RANDOM_RANGE = "E205"
    INVALID_FUNCTION_ARG = "E206"

    # --- generation --------------------------------------------------------
    MISSING_VARIABLE = "E301"
    MISSING_EXTERNAL_VARIABLE = "E302"
    FUNCTION_FAILED = "E303"
    UNIQUE_POOL_EXHAUSTED = "E304"

    # --- security / limits -------------------------------------------------
    INPUT_TOO_LARGE = "E401"
    TREE_TOO_DEEP = "E402"
    TREE_TOO_LARGE = "E403"
    OUTPUT_TOO_LARGE = "E404"
    BATCH_TOO_LARGE = "E405"
    MACRO_TOO_DEEP = "E406"
    TOO_MANY_STEPS = "E407"

    # --- warnings ----------------------------------------------------------
    UNUSED_MACRO = "W901"
    UNASSIGNED_VARIABLE = "W902"


@dataclass(frozen=True, slots=True)
class CodeInfo:
    """Human-facing description attached to an :class:`ErrorCode`."""

    title: str
    cause: str
    fix_example: str


CODE_CATALOG: dict[ErrorCode, CodeInfo] = {
    ErrorCode.DANGLING_ESCAPE: CodeInfo(
        "Dangling escape at end of input",
        "A backslash must be followed by the character it escapes.",
        r"Write '\\' to output a literal backslash.",
    ),
    ErrorCode.UNCLOSED_BRACE: CodeInfo(
        "Unclosed '{' construct",
        "A '{' was opened but the matching '}' is missing.",
        r"{A|B}  — or escape the brace: \{",
    ),
    ErrorCode.UNCLOSED_BRACKET: CodeInfo(
        "Unclosed '[' construct",
        "A '[' was opened but the matching ']' is missing.",
        r"[rand:1:100]  — or escape the bracket: \[",
    ),
    ErrorCode.UNEXPECTED_CLOSING: CodeInfo(
        "Unexpected closing delimiter",
        "A '}' or ']' appears without a matching opening delimiter.",
        r"Escape it to output it literally: \} or \]",
    ),
    ErrorCode.EMPTY_CHOICE: CodeInfo(
        "Empty choice construct",
        "'{}' contains no variants.",
        r"{A|B}  — or escape the braces: \{\}",
    ),
    ErrorCode.STRAY_PIPE: CodeInfo(
        "'|' outside of a choice construct",
        "The pipe character only separates variants inside '{...}'.",
        r"Escape it to output it literally: \|",
    ),
    ErrorCode.UNKNOWN_BRACKET: CodeInfo(
        "Unrecognized '[...]' construct",
        "Content of '[...]' is neither an optional block nor a function call.",
        r"[50%:maybe ] or [uuid] — or escape the bracket: \[",
    ),
    ErrorCode.INVALID_WEIGHT: CodeInfo(
        "Invalid variant weight",
        "The value after '::' must be a positive number.",
        "{Apple::10|Orange::3}",
    ),
    ErrorCode.INVALID_PERCENT: CodeInfo(
        "Invalid optional probability",
        "The value before '%' must be a number between 0 and 100.",
        "[25%:sometimes ]",
    ),
    ErrorCode.BAD_FUNCTION_ARITY: CodeInfo(
        "Wrong number of function arguments",
        "The function was called with an unsupported number of arguments.",
        "[rand:1:100]",
    ),
    ErrorCode.PIPE_IN_ASSIGNMENT: CodeInfo(
        "'|' at the top level of an assignment",
        "An assignment value cannot contain top-level variants.",
        "{name={John|Jane}}",
    ),
    ErrorCode.EMPTY_MACRO_BODY: CodeInfo(
        "Empty macro body",
        "A macro definition must have a non-empty body.",
        "#hello = {Hi|Hello}",
    ),
    ErrorCode.WEIGHT_IN_SHUFFLE: CodeInfo(
        "Weights are not allowed in a shuffle",
        "'{~...}' outputs every variant exactly once, so weights are meaningless.",
        "{~A|B|C}",
    ),
    ErrorCode.UNDEFINED_MACRO: CodeInfo(
        "Undefined macro",
        "The macro is called but never defined in this template.",
        "#hello = {Hi|Hello}\n#hello",
    ),
    ErrorCode.MACRO_CYCLE: CodeInfo(
        "Cyclic macro definition",
        "Macros call each other in a cycle, which would never terminate.",
        "Remove the recursive call from one of the macros.",
    ),
    ErrorCode.MACRO_REDEFINED: CodeInfo(
        "Macro redefined",
        "A macro with this name was already defined in this template.",
        "Rename one of the definitions.",
    ),
    ErrorCode.UNKNOWN_FUNCTION: CodeInfo(
        "Unknown function",
        "No built-in or registered function has this name.",
        "engine.register_function('upper', handler)",
    ),
    ErrorCode.INVALID_RANDOM_RANGE: CodeInfo(
        "Invalid random range",
        "The lower bound must not exceed the upper bound.",
        "[rand:1:100]",
    ),
    ErrorCode.INVALID_FUNCTION_ARG: CodeInfo(
        "Invalid function argument",
        "An argument does not match the type the function expects.",
        "[date:+5]",
    ),
    ErrorCode.MISSING_VARIABLE: CodeInfo(
        "Missing variable",
        "The variable is read before any '{name=value}' assignment.",
        "{name=John} Hello $name",
    ),
    ErrorCode.MISSING_EXTERNAL_VARIABLE: CodeInfo(
        "Missing external variable",
        "The variable was not provided through the generation Context.",
        "Context(variables={'username': 'john'})",
    ),
    ErrorCode.FUNCTION_FAILED: CodeInfo(
        "Function call failed",
        "A registered function raised an exception at generation time.",
        "Check the function implementation and its arguments.",
    ),
    ErrorCode.UNIQUE_POOL_EXHAUSTED: CodeInfo(
        "Unable to produce enough unique results",
        "The template does not have enough distinct outcomes for the batch.",
        "Reduce the batch size or add more variants.",
    ),
    ErrorCode.INPUT_TOO_LARGE: CodeInfo(
        "Template is too large",
        "The template exceeds Limits.max_input_length.",
        "Raise the limit: Limits(max_input_length=...)",
    ),
    ErrorCode.TREE_TOO_DEEP: CodeInfo(
        "Template nesting is too deep",
        "Nesting exceeds Limits.max_nesting_depth.",
        "Flatten the template or raise the limit.",
    ),
    ErrorCode.TREE_TOO_LARGE: CodeInfo(
        "Template has too many nodes",
        "The parsed tree exceeds Limits.max_nodes.",
        "Split the template or raise the limit.",
    ),
    ErrorCode.OUTPUT_TOO_LARGE: CodeInfo(
        "Generated output is too large",
        "Generation exceeded Limits.max_output_length.",
        "Raise the limit: Limits(max_output_length=...)",
    ),
    ErrorCode.BATCH_TOO_LARGE: CodeInfo(
        "Batch is too large",
        "The requested count exceeds Limits.max_batch_size.",
        "Raise the limit: Limits(max_batch_size=...)",
    ),
    ErrorCode.MACRO_TOO_DEEP: CodeInfo(
        "Macro expansion is too deep",
        "Macro expansion exceeded Limits.max_macro_depth.",
        "Reduce macro nesting or raise the limit.",
    ),
    ErrorCode.TOO_MANY_STEPS: CodeInfo(
        "Generation step budget exceeded",
        "Generation exceeded Limits.max_generation_steps.",
        "Simplify the template or raise the limit.",
    ),
    ErrorCode.UNUSED_MACRO: CodeInfo(
        "Macro defined but never used",
        "The macro definition has no matching call.",
        "Remove the definition or call it with #name.",
    ),
    ErrorCode.UNASSIGNED_VARIABLE: CodeInfo(
        "Variable is never assigned",
        "'$name' is read but no '{name=...}' assignment exists.",
        "{name=John} ... $name",
    ),
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single problem found in a template."""

    severity: Severity
    code: ErrorCode
    message: str
    span: SourceSpan
    cause: str = ""
    fix_example: str = ""

    @staticmethod
    def make(
        severity: Severity,
        code: ErrorCode,
        message: str,
        span: SourceSpan,
    ) -> "Diagnostic":
        info = CODE_CATALOG[code]
        return Diagnostic(
            severity=severity,
            code=code,
            message=message,
            span=span,
            cause=info.cause,
            fix_example=info.fix_example,
        )

    @staticmethod
    def error(code: ErrorCode, message: str, span: SourceSpan) -> "Diagnostic":
        return Diagnostic.make(Severity.ERROR, code, message, span)

    @staticmethod
    def warning(code: ErrorCode, message: str, span: SourceSpan) -> "Diagnostic":
        return Diagnostic.make(Severity.WARNING, code, message, span)


class SpintaxError(Exception):
    """Base class for every exception raised by the engine."""

    def __init__(self, diagnostic: Diagnostic, source: str | None = None) -> None:
        super().__init__(f"[{diagnostic.code.value}] {diagnostic.message}")
        self.diagnostic = diagnostic
        self.source = source


class TemplateSyntaxError(SpintaxError):
    """Raised by the lexer or parser on malformed input."""


class SemanticError(SpintaxError):
    """Raised when a structurally valid template is semantically invalid."""


class GenerationError(SpintaxError):
    """Raised at generation time (missing variables, failing functions, ...)."""


class LimitExceededError(SpintaxError):
    """Raised when a configured security limit is exceeded."""


class ValidationFailedError(SpintaxError):
    """Raised by APIs that require a valid template when validation fails."""

    def __init__(self, diagnostics: list[Diagnostic], source: str | None = None) -> None:
        first = diagnostics[0]
        super().__init__(first, source)
        self.diagnostics = diagnostics


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of :meth:`SpintaxEngine.validate`."""

    diagnostics: tuple[Diagnostic, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity is Severity.WARNING)
