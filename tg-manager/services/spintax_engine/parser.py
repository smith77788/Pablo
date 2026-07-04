"""Parser: recursive descent over the token stream, producing the AST.

The parser never touches the source string directly — it works purely on
tokens.  Structural tokens that carry no meaning in the current position
(a stray ``:`` in prose, ``=`` inside a variant, ...) are folded back into
literal text, so users only ever need to escape the eight documented
metacharacters.
"""

from __future__ import annotations

from .errors import (
    Diagnostic,
    ErrorCode,
    LimitExceededError,
    SourceSpan,
    TemplateSyntaxError,
)
from .lexer import tokenize
from .limits import DEFAULT_LIMITS, Limits
from .nodes import (
    AssignmentNode,
    ChoiceNode,
    DateNode,
    DateTimeNode,
    EscapeNode,
    ExternalVariableNode,
    FunctionCallNode,
    MacroCallNode,
    MacroDefinitionNode,
    Node,
    OptionalNode,
    PickNode,
    RandomFloatNode,
    RandomNode,
    RootNode,
    SequenceNode,
    ShuffleNode,
    TextNode,
    TimeNode,
    TimestampNode,
    UnixNode,
    UUIDNode,
    UniqueNode,
    VariableNode,
    Variant,
    WeightedChoiceNode,
)
from .tokens import Token, TokenType

_T = TokenType

_SEQUENCE_STOPS = frozenset(
    {_T.EOF, _T.RBRACE, _T.RBRACKET, _T.PIPE, _T.DOUBLE_COLON, _T.COLON, _T.NEWLINE}
)

# Bracket functions with dedicated AST node types: name -> (node factory, min, max).
_ZERO_ARG_BRACKETS: dict[str, type[Node]] = {
    "uuid": UUIDNode,
    "timestamp": TimestampNode,
    "unix": UnixNode,
    "time": TimeNode,
    "datetime": DateTimeNode,
}


def _is_identifier(text: str) -> bool:
    return text.isidentifier()


class Parser:
    """Parses one token stream.  Instances are single-use.

    ``lenient=True`` downgrades stray structural characters (``}``, ``]``,
    ``|``, unknown ``[...]``) to literal text instead of raising, which is
    convenient for user-facing bots.  Strict mode is the default.
    """

    def __init__(
        self,
        tokens: list[Token],
        source: str,
        limits: Limits = DEFAULT_LIMITS,
        lenient: bool = False,
    ) -> None:
        self._tokens = tokens
        self._source = source
        self._limits = limits
        self._lenient = lenient
        self._pos = 0
        self._depth = 0
        self._node_count = 0
        self._macros: list[MacroDefinitionNode] = []

    # --- public ------------------------------------------------------------

    def parse(self) -> RootNode:
        body = self._parse_sequence(stops=frozenset({_T.EOF}), top_level=True)
        root_span = SourceSpan(0, max(len(self._source), 1), 1, 1)
        return RootNode(span=root_span, body=body, macros=tuple(self._macros))

    # --- token plumbing -----------------------------------------------------

    def _peek(self, ahead: int = 0) -> Token:
        pos = min(self._pos + ahead, len(self._tokens) - 1)
        return self._tokens[pos]

    def _next(self) -> Token:
        token = self._tokens[self._pos]
        if token.type is not _T.EOF:
            self._pos += 1
        return token

    def _error(self, code: ErrorCode, message: str, span: SourceSpan) -> TemplateSyntaxError:
        return TemplateSyntaxError(Diagnostic.error(code, message, span), self._source)

    def _make(self, cls: type, span: SourceSpan, **kwargs: object) -> Node:
        self._node_count += 1
        if self._node_count > self._limits.max_nodes:
            raise LimitExceededError(
                Diagnostic.error(
                    ErrorCode.TREE_TOO_LARGE,
                    f"Template exceeds {self._limits.max_nodes} AST nodes",
                    span,
                ),
                self._source,
            )
        return cls(span=span, **kwargs)

    # --- sequences -----------------------------------------------------------

    def _parse_sequence(
        self,
        stops: frozenset[TokenType],
        top_level: bool = False,
    ) -> SequenceNode:
        start = self._peek().span
        children: list[Node] = []
        text_buf: list[str] = []
        text_span: SourceSpan | None = None

        def flush() -> None:
            nonlocal text_span
            if text_buf:
                children.append(
                    self._make(TextNode, text_span or start, text="".join(text_buf))
                )
                text_buf.clear()
                text_span = None

        def literal(token: Token) -> None:
            nonlocal text_span
            if text_span is None:
                text_span = token.span
            text_buf.append(token.raw)

        while True:
            token = self._peek()
            ttype = token.type
            if ttype in stops:
                break
            if ttype is _T.EOF:
                break  # caller decides whether this is an "unclosed" error
            if ttype in (_T.TEXT, _T.NEWLINE) or ttype not in _SEQUENCE_STOPS:
                handled = self._parse_item(token, children, flush, literal, top_level)
                if not handled:
                    break
            elif ttype in (_T.RBRACE, _T.RBRACKET):
                if self._lenient:
                    literal(self._next())
                else:
                    raise self._error(
                        ErrorCode.UNEXPECTED_CLOSING,
                        f"Unexpected '{token.raw}' without a matching opener",
                        token.span,
                    )
            elif ttype is _T.PIPE:
                if self._lenient:
                    literal(self._next())
                else:
                    raise self._error(
                        ErrorCode.STRAY_PIPE,
                        "'|' is only valid inside a '{...}' choice",
                        token.span,
                    )
            else:  # COLON / DOUBLE_COLON / NEWLINE outside their constructs
                literal(self._next())
        flush()
        return SequenceNode(span=start, children=tuple(children))

    def _parse_item(
        self,
        token: Token,
        children: list[Node],
        flush,
        literal,
        top_level: bool,
    ) -> bool:
        """Parse one non-stop token into ``children``. Returns False to stop."""
        ttype = token.type
        if ttype in (_T.TEXT, _T.NEWLINE):
            literal(self._next())
        elif ttype is _T.ESCAPED:
            flush()
            self._next()
            children.append(self._make(EscapeNode, token.span, char=token.value))
        elif ttype is _T.LBRACE:
            flush()
            children.append(self._parse_brace())
        elif ttype is _T.LBRACKET:
            flush()
            children.append(self._parse_bracket())
        elif ttype is _T.DOLLAR_IDENT:
            flush()
            self._next()
            children.append(self._make(VariableNode, token.span, name=token.value))
        elif ttype is _T.EXTERNAL_VAR:
            flush()
            self._next()
            children.append(self._make(ExternalVariableNode, token.span, name=token.value))
        elif ttype is _T.HASH_IDENT:
            flush()
            children.append(self._parse_hash(top_level))
        else:  # TILDE / BANG / EQUALS / PERCENT with no structural meaning here
            literal(self._next())
        return True

    # --- nesting guard --------------------------------------------------------

    def _enter(self, span: SourceSpan) -> None:
        self._depth += 1
        if self._depth > self._limits.max_nesting_depth:
            raise LimitExceededError(
                Diagnostic.error(
                    ErrorCode.TREE_TOO_DEEP,
                    f"Nesting exceeds {self._limits.max_nesting_depth} levels",
                    span,
                ),
                self._source,
            )

    def _exit(self) -> None:
        self._depth -= 1

    # --- braces: choices, assignments ------------------------------------------

    def _parse_brace(self) -> Node:
        lbrace = self._next()
        self._enter(lbrace.span)
        try:
            if self._is_assignment_ahead():
                return self._parse_assignment(lbrace)
            return self._parse_choice(lbrace)
        finally:
            self._exit()

    def _is_assignment_ahead(self) -> bool:
        first = self._peek()
        return (
            first.type is _T.TEXT
            and _is_identifier(first.value.strip())
            and self._peek(1).type is _T.EQUALS
        )

    def _parse_assignment(self, lbrace: Token) -> Node:
        name = self._next().value.strip()
        self._next()  # '='
        value = self._parse_sequence(stops=frozenset({_T.RBRACE, _T.PIPE, _T.EOF}))
        closing = self._peek()
        if closing.type is _T.PIPE:
            raise self._error(
                ErrorCode.PIPE_IN_ASSIGNMENT,
                "'|' cannot appear at the top level of an assignment; "
                "wrap the alternatives in braces",
                closing.span,
            )
        if closing.type is not _T.RBRACE:
            raise self._error(
                ErrorCode.UNCLOSED_BRACE, "Missing '}' for this assignment", lbrace.span
            )
        self._next()
        return self._make(AssignmentNode, lbrace.span, name=name, value=value)

    def _parse_choice(self, lbrace: Token) -> Node:
        mode = self._peek().type
        if mode in (_T.TILDE, _T.BANG):
            self._next()
        else:
            mode = None

        variants: list[Node] = []
        has_weight = False
        while True:
            content = self._parse_sequence(
                stops=frozenset({_T.PIPE, _T.RBRACE, _T.DOUBLE_COLON, _T.EOF})
            )
            weight: float | None = None
            if self._peek().type is _T.DOUBLE_COLON:
                weight = self._parse_weight()
                has_weight = True
            variants.append(
                self._make(Variant, content.span, content=content, weight=weight)
            )
            separator = self._peek()
            if separator.type is _T.PIPE:
                self._next()
                continue
            if separator.type is _T.RBRACE:
                self._next()
                break
            raise self._error(
                ErrorCode.UNCLOSED_BRACE, "Missing '}' for this construct", lbrace.span
            )

        if len(variants) == 1 and not variants[0].content.children and mode is None:
            raise self._error(
                ErrorCode.EMPTY_CHOICE, "'{}' contains no variants", lbrace.span
            )
        if mode is _T.TILDE:
            if has_weight:
                raise self._error(
                    ErrorCode.WEIGHT_IN_SHUFFLE,
                    "Weights ('::') are not allowed inside '{~...}'",
                    lbrace.span,
                )
            return self._make(ShuffleNode, lbrace.span, variants=tuple(variants))
        if mode is _T.BANG:
            return self._make(UniqueNode, lbrace.span, variants=tuple(variants))
        if has_weight:
            return self._make(WeightedChoiceNode, lbrace.span, variants=tuple(variants))
        return self._make(ChoiceNode, lbrace.span, variants=tuple(variants))

    def _parse_weight(self) -> float:
        marker = self._next()  # '::'
        token = self._peek()
        if token.type is not _T.TEXT:
            raise self._error(
                ErrorCode.INVALID_WEIGHT, "Expected a number after '::'", marker.span
            )
        raw = token.value.strip()
        try:
            weight = float(raw)
        except ValueError:
            raise self._error(
                ErrorCode.INVALID_WEIGHT, f"'{raw}' is not a valid weight", token.span
            ) from None
        if not weight > 0:
            raise self._error(
                ErrorCode.INVALID_WEIGHT, "Weights must be positive", token.span
            )
        self._next()
        after = self._peek()
        if after.type not in (_T.PIPE, _T.RBRACE):
            raise self._error(
                ErrorCode.INVALID_WEIGHT,
                "A weight must be the last thing in its variant",
                after.span,
            )
        return weight

    # --- brackets: optionals, built-ins, functions --------------------------------

    def _parse_bracket(self) -> Node:
        lbracket = self._next()
        self._enter(lbracket.span)
        try:
            first = self._peek()
            if first.type is _T.TEXT and self._peek(1).type is _T.PERCENT:
                return self._parse_optional(lbracket)
            if first.type is _T.TEXT and _is_identifier(first.value.strip()):
                nxt = self._peek(1).type
                if nxt in (_T.COLON, _T.RBRACKET):
                    return self._parse_function(lbracket)
            if self._lenient:
                return self._make(TextNode, lbracket.span, text="[")
            raise self._error(
                ErrorCode.UNKNOWN_BRACKET,
                "Expected an optional block '[N%:...]' or a function call '[name:...]'",
                lbracket.span,
            )
        finally:
            self._exit()

    def _parse_optional(self, lbracket: Token) -> Node:
        number = self._next()
        percent = self._next()
        try:
            value = float(number.value.strip())
        except ValueError:
            raise self._error(
                ErrorCode.INVALID_PERCENT,
                f"'{number.value.strip()}' is not a valid probability",
                number.span,
            ) from None
        if not 0 <= value <= 100:
            raise self._error(
                ErrorCode.INVALID_PERCENT,
                "The probability must be between 0 and 100",
                number.span,
            )
        if self._peek().type is not _T.COLON:
            raise self._error(
                ErrorCode.INVALID_PERCENT,
                "Expected ':' after the probability, e.g. [25%:text]",
                percent.span,
            )
        self._next()
        content = self._parse_sequence(stops=frozenset({_T.RBRACKET, _T.EOF}))
        if self._peek().type is not _T.RBRACKET:
            raise self._error(
                ErrorCode.UNCLOSED_BRACKET, "Missing ']' for this block", lbracket.span
            )
        self._next()
        return self._make(
            OptionalNode, lbracket.span, probability=value / 100.0, content=content
        )

    def _parse_function(self, lbracket: Token) -> Node:
        name_token = self._next()
        name = name_token.value.strip()
        args: list[SequenceNode] = []
        while self._peek().type is _T.COLON:
            self._next()
            args.append(self._parse_sequence(stops=frozenset({_T.COLON, _T.RBRACKET, _T.EOF})))
        if self._peek().type is not _T.RBRACKET:
            raise self._error(
                ErrorCode.UNCLOSED_BRACKET, "Missing ']' for this call", lbracket.span
            )
        self._next()
        return self._build_bracket_node(lbracket, name, tuple(args))

    def _build_bracket_node(
        self, lbracket: Token, name: str, args: tuple[SequenceNode, ...]
    ) -> Node:
        span = lbracket.span

        def require(count_min: int, count_max: int) -> None:
            if not count_min <= len(args) <= count_max:
                expected = (
                    str(count_min)
                    if count_min == count_max
                    else f"{count_min}..{count_max}"
                )
                raise self._error(
                    ErrorCode.BAD_FUNCTION_ARITY,
                    f"[{name}] expects {expected} argument(s), got {len(args)}",
                    span,
                )

        if name in _ZERO_ARG_BRACKETS:
            require(0, 0)
            return self._make(_ZERO_ARG_BRACKETS[name], span)
        if name == "rand":
            require(2, 2)
            return self._make(RandomNode, span, low=args[0], high=args[1])
        if name == "randf":
            require(2, 2)
            return self._make(RandomFloatNode, span, low=args[0], high=args[1])
        if name == "pick":
            require(1, 1)
            return self._make(PickNode, span, items=args[0])
        if name == "date":
            require(0, 1)
            return self._make(DateNode, span, offset=args[0] if args else None)
        return self._make(FunctionCallNode, span, name=name, args=args)

    # --- macros -------------------------------------------------------------------

    def _parse_hash(self, top_level: bool) -> Node:
        token = self._next()
        if top_level and self._is_definition_ahead():
            return self._parse_macro_definition(token)
        return self._make(MacroCallNode, token.span, name=token.value)

    def _is_definition_ahead(self) -> bool:
        first = self._peek()
        if first.type is _T.EQUALS:
            return True
        return (
            first.type is _T.TEXT
            and not first.value.strip()
            and self._peek(1).type is _T.EQUALS
        )

    def _parse_macro_definition(self, token: Token) -> Node:
        if self._peek().type is _T.TEXT:
            self._next()  # whitespace between name and '='
        self._next()  # '='
        body = self._trim_sequence(
            self._parse_sequence(stops=frozenset({_T.NEWLINE, _T.EOF}))
        )
        if not body.children:
            raise self._error(
                ErrorCode.EMPTY_MACRO_BODY,
                f"Macro '#{token.value}' has an empty body",
                token.span,
            )
        if self._peek().type is _T.NEWLINE:
            self._next()  # the definition swallows its own line break
        node = self._make(MacroDefinitionNode, token.span, name=token.value, body=body)
        self._macros.append(node)
        return node

    def _trim_sequence(self, sequence: SequenceNode) -> SequenceNode:
        children = list(sequence.children)
        if children and isinstance(children[0], TextNode):
            stripped = children[0].text.lstrip()
            if stripped != children[0].text:
                children[0] = TextNode(span=children[0].span, text=stripped)
        if children and isinstance(children[-1], TextNode):
            stripped = children[-1].text.rstrip()
            if stripped != children[-1].text:
                children[-1] = TextNode(span=children[-1].span, text=stripped)
        children = [c for c in children if not (isinstance(c, TextNode) and not c.text)]
        return SequenceNode(span=sequence.span, children=tuple(children))


def parse(
    source: str,
    limits: Limits = DEFAULT_LIMITS,
    lenient: bool = False,
) -> RootNode:
    """Tokenize and parse ``source`` into an AST."""
    return Parser(tokenize(source, limits), source, limits, lenient).parse()
