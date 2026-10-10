"""Error reporter: renders diagnostics as human-readable text blocks.

Example output::

    error[E101]: Unclosed '{' construct
     --> line 1, column 7
      |
    1 | Hello {A|B
      |       ^
      = cause: A '{' was opened but the matching '}' is missing.
      = fix:   {A|B}  — or escape the brace: \\{
"""

from __future__ import annotations

from .errors import Diagnostic, SpintaxError


class ErrorReporter:
    """Formats :class:`Diagnostic` objects against the template source."""

    def __init__(self, source: str | None = None) -> None:
        self._source = source

    def format(self, diagnostic: Diagnostic, source: str | None = None) -> str:
        source = source if source is not None else self._source
        lines = [
            f"{diagnostic.severity.value}[{diagnostic.code.value}]: {diagnostic.message}",
            f" --> line {diagnostic.span.line}, column {diagnostic.span.column}",
        ]
        snippet = self._snippet(diagnostic, source)
        if snippet:
            lines.extend(snippet)
        if diagnostic.cause:
            lines.append(f"  = cause: {diagnostic.cause}")
        if diagnostic.fix_example:
            fix = diagnostic.fix_example.replace("\n", "\n           ")
            lines.append(f"  = fix:   {fix}")
        return "\n".join(lines)

    def format_many(
        self, diagnostics: list[Diagnostic] | tuple[Diagnostic, ...], source: str | None = None
    ) -> str:
        return "\n\n".join(self.format(d, source) for d in diagnostics)

    def format_exception(self, error: SpintaxError) -> str:
        return self.format(error.diagnostic, error.source)

    # --- helpers -------------------------------------------------------------

    def _snippet(self, diagnostic: Diagnostic, source: str | None) -> list[str]:
        if source is None:
            return []
        source_lines = source.splitlines()
        index = diagnostic.span.line - 1
        if not 0 <= index < len(source_lines):
            return []
        line_text = source_lines[index]
        number = str(diagnostic.span.line)
        gutter = " " * len(number)
        caret_pos = max(diagnostic.span.column - 1, 0)
        caret_len = max(min(diagnostic.span.length, len(line_text) - caret_pos), 1)
        return [
            f"{gutter} |",
            f"{number} | {line_text}",
            f"{gutter} | {' ' * caret_pos}{'^' * caret_len}",
        ]
