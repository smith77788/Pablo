"""Lexer: a single O(n) pass turning the template into a flat token stream.

The lexer is *minimally* context-sensitive: it tracks the current brace and
bracket depth so that characters such as ``:``, ``%``, ``=``, ``~`` and ``!``
are emitted as structural tokens only where the grammar can give them meaning.
Everywhere else they are emitted as plain ``TEXT``, which keeps ordinary prose
("see 10:30", "50% off") free of escaping.
"""

from __future__ import annotations

from .errors import Diagnostic, ErrorCode, SourceSpan, TemplateSyntaxError
from .limits import DEFAULT_LIMITS, Limits
from .tokens import Token, TokenType

_SPECIALS = frozenset("{}[]|\\$#\n:=%~!")


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class Lexer:
    """Tokenizes one template string.  Instances are single-use."""

    def __init__(self, source: str, limits: Limits = DEFAULT_LIMITS) -> None:
        self._source = source
        self._limits = limits
        self._pos = 0
        self._line = 1
        self._column = 1
        self._brace_depth = 0
        self._bracket_depth = 0
        self._tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        source = self._source
        if len(source) > self._limits.max_input_length:
            raise TemplateSyntaxError(
                Diagnostic.error(
                    ErrorCode.INPUT_TOO_LARGE,
                    f"Template is {len(source)} characters long; "
                    f"the limit is {self._limits.max_input_length}",
                    SourceSpan.point(0, 1, 1),
                ),
                source,
            )
        while self._pos < len(source):
            ch = source[self._pos]
            if ch in _SPECIALS:
                self._lex_special(ch)
            else:
                self._lex_text()
        self._emit(TokenType.EOF, "", "", self._pos)
        return self._tokens

    # --- helpers -----------------------------------------------------------

    def _span(self, start: int, length: int) -> SourceSpan:
        return SourceSpan(start, max(length, 1), self._line, self._column)

    def _emit(self, type_: TokenType, value: str, raw: str, start: int) -> None:
        self._tokens.append(Token(type_, value, raw, self._span(start, len(raw))))
        self._advance(raw)

    def _advance(self, raw: str) -> None:
        newlines = raw.count("\n")
        if newlines:
            self._line += newlines
            self._column = len(raw) - raw.rfind("\n")
        else:
            self._column += len(raw)
        self._pos += len(raw)

    def _emit_text(self, raw: str, start: int) -> None:
        self._emit(TokenType.TEXT, raw, raw, start)

    def _peek(self, ahead: int = 1) -> str:
        pos = self._pos + ahead
        return self._source[pos] if pos < len(self._source) else ""

    def _scan_ident(self, start: int) -> str:
        source = self._source
        if start >= len(source) or not _is_ident_start(source[start]):
            return ""
        end = start + 1
        while end < len(source) and _is_ident_char(source[end]):
            end += 1
        return source[start:end]

    def _last_types(self) -> tuple[TokenType | None, TokenType | None, bool]:
        """Return (last, before_last, last_text_is_blank) of emitted tokens."""
        last = self._tokens[-1] if self._tokens else None
        prev = self._tokens[-2] if len(self._tokens) > 1 else None
        blank = last is not None and last.type is TokenType.TEXT and not last.value.strip()
        return (
            last.type if last else None,
            prev.type if prev else None,
            blank,
        )

    # --- dispatch ----------------------------------------------------------

    def _lex_special(self, ch: str) -> None:
        pos = self._pos
        if ch == "\\":
            self._lex_escape(pos)
        elif ch == "\n":
            self._emit(TokenType.NEWLINE, "\n", "\n", pos)
        elif ch == "{":
            self._lex_open_brace(pos)
        elif ch == "}":
            self._brace_depth = max(0, self._brace_depth - 1)
            self._emit(TokenType.RBRACE, "}", "}", pos)
        elif ch == "[":
            self._bracket_depth += 1
            self._emit(TokenType.LBRACKET, "[", "[", pos)
        elif ch == "]":
            self._bracket_depth = max(0, self._bracket_depth - 1)
            self._emit(TokenType.RBRACKET, "]", "]", pos)
        elif ch == "|":
            self._emit(TokenType.PIPE, "|", "|", pos)
        elif ch == "$":
            self._lex_sigil(pos, TokenType.DOLLAR_IDENT, "$")
        elif ch == "#":
            self._lex_sigil(pos, TokenType.HASH_IDENT, "#")
        elif ch == ":":
            self._lex_colon(pos)
        elif ch == "%":
            if self._bracket_depth > 0:
                self._emit(TokenType.PERCENT, "%", "%", pos)
            else:
                self._emit_text("%", pos)
        elif ch == "=":
            self._lex_equals(pos)
        elif ch in "~!":
            last, _, _ = self._last_types()
            if last is TokenType.LBRACE:
                type_ = TokenType.TILDE if ch == "~" else TokenType.BANG
                self._emit(type_, ch, ch, pos)
            else:
                self._emit_text(ch, pos)

    def _lex_escape(self, pos: int) -> None:
        nxt = self._peek()
        if not nxt:
            raise TemplateSyntaxError(
                Diagnostic.error(
                    ErrorCode.DANGLING_ESCAPE,
                    "The template ends with a lone backslash",
                    SourceSpan.point(pos, self._line, self._column),
                ),
                self._source,
            )
        self._emit(TokenType.ESCAPED, nxt, "\\" + nxt, pos)

    def _lex_open_brace(self, pos: int) -> None:
        # '{{ident}}' is an external variable; anything else is a plain '{'.
        if self._peek() == "{":
            ident = self._scan_ident(pos + 2)
            end = pos + 2 + len(ident)
            if ident and self._source[end : end + 2] == "}}":
                raw = self._source[pos : end + 2]
                self._emit(TokenType.EXTERNAL_VAR, ident, raw, pos)
                return
        self._brace_depth += 1
        self._emit(TokenType.LBRACE, "{", "{", pos)

    def _lex_sigil(self, pos: int, type_: TokenType, sigil: str) -> None:
        ident = self._scan_ident(pos + 1)
        if ident:
            self._emit(type_, ident, sigil + ident, pos)
        else:
            self._emit_text(sigil, pos)

    def _lex_colon(self, pos: int) -> None:
        # Inside '[...]' a colon is always an argument separator; the '::'
        # weight marker only exists at brace level.
        if self._bracket_depth > 0:
            self._emit(TokenType.COLON, ":", ":", pos)
        elif self._peek() == ":" and self._brace_depth > 0:
            self._emit(TokenType.DOUBLE_COLON, "::", "::", pos)
        else:
            self._emit_text(":", pos)

    def _lex_equals(self, pos: int) -> None:
        last, prev, last_blank = self._last_types()
        after_macro = last is TokenType.HASH_IDENT or (
            last_blank and prev is TokenType.HASH_IDENT
        )
        if self._brace_depth > 0 or after_macro:
            self._emit(TokenType.EQUALS, "=", "=", pos)
        else:
            self._emit_text("=", pos)

    def _lex_text(self) -> None:
        source = self._source
        start = self._pos
        end = start
        while end < len(source) and source[end] not in _SPECIALS:
            end += 1
        self._emit_text(source[start:end], start)


def tokenize(source: str, limits: Limits = DEFAULT_LIMITS) -> list[Token]:
    """Convenience wrapper around :class:`Lexer`."""
    return Lexer(source, limits).tokenize()
