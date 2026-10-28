"""AST node types.

All nodes are immutable dataclasses, so a parsed tree can be cached and shared
between threads and generations without defensive copies.  The generator,
formatter and analyzer are external visitors — nodes carry no behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import SourceSpan


@dataclass(frozen=True, slots=True)
class Node:
    """Base class for every AST node."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SequenceNode(Node):
    """An ordered run of nodes evaluated left to right."""

    children: tuple[Node, ...] = ()


@dataclass(frozen=True, slots=True)
class RootNode(Node):
    """Top of the tree; also carries the macro table collected by the parser."""

    body: SequenceNode = field(default=None)  # type: ignore[assignment]
    macros: tuple["MacroDefinitionNode", ...] = ()


@dataclass(frozen=True, slots=True)
class TextNode(Node):
    text: str = ""


@dataclass(frozen=True, slots=True)
class EscapeNode(Node):
    """A backslash-escaped character; evaluates to the character itself."""

    char: str = ""


@dataclass(frozen=True, slots=True)
class Variant(Node):
    """One alternative inside a choice-like construct."""

    content: SequenceNode = field(default=None)  # type: ignore[assignment]
    weight: float | None = None


@dataclass(frozen=True, slots=True)
class ChoiceNode(Node):
    """``{A|B|C}`` — uniform random choice."""

    variants: tuple[Variant, ...] = ()


@dataclass(frozen=True, slots=True)
class WeightedChoiceNode(Node):
    """``{A::10|B::3}`` — weighted random choice (missing weights default to 1)."""

    variants: tuple[Variant, ...] = ()


@dataclass(frozen=True, slots=True)
class ShuffleNode(Node):
    """``{~A|B|C}`` — every variant exactly once, in random order."""

    variants: tuple[Variant, ...] = ()


@dataclass(frozen=True, slots=True)
class UniqueNode(Node):
    """``{!A|B|C}`` — choice without replacement within one generation."""

    variants: tuple[Variant, ...] = ()


@dataclass(frozen=True, slots=True)
class OptionalNode(Node):
    """``[25%:text]`` — emit ``content`` with the given probability (0..1)."""

    probability: float = 0.0
    content: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class AssignmentNode(Node):
    """``{name=value}`` — evaluate ``value`` once and bind it to ``name``."""

    name: str = ""
    value: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class VariableNode(Node):
    """``$name`` — read a variable assigned earlier in this generation."""

    name: str = ""


@dataclass(frozen=True, slots=True)
class ExternalVariableNode(Node):
    """``{{name}}`` — read a variable supplied through the Context."""

    name: str = ""


@dataclass(frozen=True, slots=True)
class MacroDefinitionNode(Node):
    """``#name = body`` — define a macro; produces no output."""

    name: str = ""
    body: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class MacroCallNode(Node):
    """``#name`` — expand a macro (re-evaluated on every call)."""

    name: str = ""


@dataclass(frozen=True, slots=True)
class RandomNode(Node):
    """``[rand:low:high]`` — uniform integer in [low, high]."""

    low: SequenceNode = field(default=None)  # type: ignore[assignment]
    high: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class RandomFloatNode(Node):
    """``[randf:low:high]`` — uniform float in [low, high]."""

    low: SequenceNode = field(default=None)  # type: ignore[assignment]
    high: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class PickNode(Node):
    """``[pick:A,B,C]`` — random element of a comma-separated list."""

    items: SequenceNode = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class DateNode(Node):
    """``[date]`` / ``[date:+5]`` — current date, optionally offset in days."""

    offset: SequenceNode | None = None


@dataclass(frozen=True, slots=True)
class TimeNode(Node):
    """``[time]`` — current time."""


@dataclass(frozen=True, slots=True)
class DateTimeNode(Node):
    """``[datetime]`` — current date and time."""


@dataclass(frozen=True, slots=True)
class UUIDNode(Node):
    """``[uuid]`` — random UUID4 (seed-deterministic)."""


@dataclass(frozen=True, slots=True)
class TimestampNode(Node):
    """``[timestamp]`` — ISO 8601 timestamp."""


@dataclass(frozen=True, slots=True)
class UnixNode(Node):
    """``[unix]`` — Unix time in seconds."""


@dataclass(frozen=True, slots=True)
class FunctionCallNode(Node):
    """``[name:arg1:arg2]`` — call a registered function."""

    name: str = ""
    args: tuple[SequenceNode, ...] = ()


CHOICE_LIKE = (ChoiceNode, WeightedChoiceNode, ShuffleNode, UniqueNode)


def structural_key(node: Node) -> tuple:
    """Hashable signature of a subtree that ignores source positions.

    Two constructs written identically in different places get the same key —
    this is what lets ``{!A|B}`` groups share one no-repeat pool per generation.
    """
    from dataclasses import fields

    parts: list[object] = [type(node).__name__]
    for field_info in fields(node):
        if field_info.name == "span":
            continue
        value = getattr(node, field_info.name)
        if isinstance(value, Node):
            parts.append(structural_key(value))
        elif isinstance(value, tuple):
            parts.append(
                tuple(
                    structural_key(item) if isinstance(item, Node) else item
                    for item in value
                )
            )
        else:
            parts.append(value)
    return tuple(parts)


def iter_children(node: Node) -> tuple[Node, ...]:
    """Structural child access used by analyzer/validator walkers."""
    if isinstance(node, RootNode):
        # Macro definitions stay inside ``body`` (they matter for formatting),
        # so walking the body alone visits every node exactly once.
        return (node.body,)
    if isinstance(node, SequenceNode):
        return node.children
    if isinstance(node, CHOICE_LIKE):
        return node.variants
    if isinstance(node, Variant):
        return (node.content,)
    if isinstance(node, OptionalNode):
        return (node.content,)
    if isinstance(node, AssignmentNode):
        return (node.value,)
    if isinstance(node, MacroDefinitionNode):
        return (node.body,)
    if isinstance(node, (RandomNode, RandomFloatNode)):
        return (node.low, node.high)
    if isinstance(node, PickNode):
        return (node.items,)
    if isinstance(node, DateNode):
        return (node.offset,) if node.offset is not None else ()
    if isinstance(node, FunctionCallNode):
        return node.args
    return ()
