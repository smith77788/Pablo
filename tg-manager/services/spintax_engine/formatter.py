"""Formatter: serializes an AST back into canonical template source.

The invariant is semantic round-tripping: ``parse(format(parse(s)))``
evaluates identically to ``parse(s)``.  Escaping is conservative — every
metacharacter in literal text is backslash-escaped, plus the characters that
only matter in the current construct (``:`` inside brackets, ``=`` inside
braces, ...).
"""

from __future__ import annotations

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

_ALWAYS_ESCAPE = frozenset("{}[]|\\$#")
_IN_BRACKET = frozenset(":%")
_IN_BRACE = frozenset("=")


class Formatter:
    """Stateless except for the bracket/brace nesting flags of one render."""

    def format(self, root: RootNode) -> str:
        return self._sequence(root.body, in_bracket=False, in_brace=False)

    # --- helpers -------------------------------------------------------------

    def _escape_text(self, text: str, in_bracket: bool, in_brace: bool) -> str:
        out: list[str] = []
        for ch in text:
            if (
                ch in _ALWAYS_ESCAPE
                or (in_bracket and ch in _IN_BRACKET)
                or (in_brace and ch in _IN_BRACE)
            ):
                out.append("\\")
            out.append(ch)
        return "".join(out)

    def _sequence(self, node: SequenceNode, in_bracket: bool, in_brace: bool) -> str:
        return "".join(self._node(child, in_bracket, in_brace) for child in node.children)

    def _variants(
        self, variants: tuple[Variant, ...], with_weights: bool, in_bracket: bool
    ) -> str:
        rendered: list[str] = []
        for variant in variants:
            text = self._sequence(variant.content, in_bracket, in_brace=True)
            if with_weights and variant.weight is not None:
                text += f"::{variant.weight:g}"
            rendered.append(text)
        return "|".join(rendered)

    # --- dispatch --------------------------------------------------------------

    def _node(self, node: Node, in_bracket: bool, in_brace: bool) -> str:
        if isinstance(node, TextNode):
            return self._escape_text(node.text, in_bracket, in_brace)
        if isinstance(node, EscapeNode):
            return "\\" + node.char
        if isinstance(node, SequenceNode):
            return self._sequence(node, in_bracket, in_brace)
        if isinstance(node, ChoiceNode):
            return "{" + self._variants(node.variants, False, in_bracket) + "}"
        if isinstance(node, WeightedChoiceNode):
            return "{" + self._variants(node.variants, True, in_bracket) + "}"
        if isinstance(node, ShuffleNode):
            return "{~" + self._variants(node.variants, False, in_bracket) + "}"
        if isinstance(node, UniqueNode):
            return "{!" + self._variants(node.variants, True, in_bracket) + "}"
        if isinstance(node, OptionalNode):
            probability = node.probability * 100
            number = f"{probability:g}"
            content = self._sequence(node.content, in_bracket=True, in_brace=in_brace)
            return f"[{number}%:{content}]"
        if isinstance(node, AssignmentNode):
            value = self._sequence(node.value, in_bracket, in_brace=True)
            return "{" + node.name + "=" + value + "}"
        if isinstance(node, VariableNode):
            return "$" + node.name
        if isinstance(node, ExternalVariableNode):
            return "{{" + node.name + "}}"
        if isinstance(node, MacroDefinitionNode):
            body = self._sequence(node.body, in_bracket, in_brace)
            return f"#{node.name} = {body}\n"
        if isinstance(node, MacroCallNode):
            return "#" + node.name
        if isinstance(node, (RandomNode, RandomFloatNode)):
            name = "rand" if isinstance(node, RandomNode) else "randf"
            low = self._sequence(node.low, in_bracket=True, in_brace=in_brace)
            high = self._sequence(node.high, in_bracket=True, in_brace=in_brace)
            return f"[{name}:{low}:{high}]"
        if isinstance(node, PickNode):
            items = self._sequence(node.items, in_bracket=True, in_brace=in_brace)
            return f"[pick:{items}]"
        if isinstance(node, DateNode):
            if node.offset is None:
                return "[date]"
            offset = self._sequence(node.offset, in_bracket=True, in_brace=in_brace)
            return f"[date:{offset}]"
        if isinstance(node, TimeNode):
            return "[time]"
        if isinstance(node, DateTimeNode):
            return "[datetime]"
        if isinstance(node, UUIDNode):
            return "[uuid]"
        if isinstance(node, TimestampNode):
            return "[timestamp]"
        if isinstance(node, UnixNode):
            return "[unix]"
        if isinstance(node, FunctionCallNode):
            args = "".join(
                ":" + self._sequence(arg, in_bracket=True, in_brace=in_brace)
                for arg in node.args
            )
            return f"[{node.name}{args}]"
        raise TypeError(f"Formatter cannot render {type(node).__name__}")  # pragma: no cover
