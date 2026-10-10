"""Token model shared by the lexer and the parser."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .errors import SourceSpan


class TokenType(Enum):
    TEXT = auto()          # plain text run
    NEWLINE = auto()       # '\n' (kept separate: macro definitions end at newline)
    ESCAPED = auto()       # backslash escape; value is the escaped character
    LBRACE = auto()        # '{'
    RBRACE = auto()        # '}'
    LBRACKET = auto()      # '['
    RBRACKET = auto()      # ']'
    PIPE = auto()          # '|'
    TILDE = auto()         # '~' right after '{'
    BANG = auto()          # '!' right after '{'
    EQUALS = auto()        # '=' inside '{...}' or after '#name'
    COLON = auto()         # ':' inside '[...]'
    DOUBLE_COLON = auto()  # '::' inside '{...}'
    PERCENT = auto()       # '%' inside '[...]'
    DOLLAR_IDENT = auto()  # '$name'; value is the identifier
    HASH_IDENT = auto()    # '#name'; value is the identifier
    EXTERNAL_VAR = auto()  # '{{name}}'; value is the identifier
    EOF = auto()


@dataclass(frozen=True, slots=True)
class Token:
    type: TokenType
    value: str
    raw: str
    span: SourceSpan

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.type.name}, {self.raw!r}, @{self.span.offset})"
