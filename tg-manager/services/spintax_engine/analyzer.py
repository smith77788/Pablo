"""Analyzer: static facts about a template (size, depth, combinatorics).

Combination counting is capped at :data:`COMBINATION_CAP` so that templates
with astronomically many outcomes never trigger big-integer blowups; a capped
result is reported via ``TemplateAnalysis.is_capped``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .nodes import (
    ChoiceNode,
    DateNode,
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
    UUIDNode,
    UniqueNode,
    Variant,
    WeightedChoiceNode,
    iter_children,
)
from .semantics import static_text

COMBINATION_CAP = 10**12


@dataclass(frozen=True, slots=True)
class TemplateAnalysis:
    """Result of :meth:`SpintaxEngine.analyze`."""

    node_count: int
    max_depth: int
    combination_count: int
    is_capped: bool
    macros: frozenset[str]
    variables: frozenset[str]
    external_variables: frozenset[str]
    functions: frozenset[str]


class Analyzer:
    """Walks a validated AST; requires the macro table for call expansion."""

    def __init__(self, macros: dict[str, MacroDefinitionNode]) -> None:
        self._macros = macros
        self._memo: dict[str, int] = {}
        self._capped = False

    def analyze(
        self,
        root: RootNode,
        variables: frozenset[str],
        external_variables: frozenset[str],
        functions: frozenset[str],
    ) -> TemplateAnalysis:
        node_count, max_depth = self._measure(root)
        combinations = self._combinations(root.body, visiting=set())
        return TemplateAnalysis(
            node_count=node_count,
            max_depth=max_depth,
            combination_count=combinations,
            is_capped=self._capped,
            macros=frozenset(self._macros),
            variables=variables,
            external_variables=external_variables,
            functions=functions,
        )

    # --- size / depth ---------------------------------------------------------

    def _measure(self, root: RootNode) -> tuple[int, int]:
        count = 0
        max_depth = 0
        stack: list[tuple[Node, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            count += 1
            max_depth = max(max_depth, depth)
            for child in iter_children(node):
                stack.append((child, depth + 1))
        return count, max_depth

    # --- combinatorics -----------------------------------------------------------

    def _cap(self, value: int) -> int:
        if value >= COMBINATION_CAP:
            self._capped = True
            return COMBINATION_CAP
        return value

    def _combinations(self, node: Node, visiting: set[str]) -> int:
        if isinstance(node, (SequenceNode, Variant)):
            children = iter_children(node)
            total = 1
            for child in children:
                total = self._cap(total * self._combinations(child, visiting))
            return total
        if isinstance(node, (ChoiceNode, WeightedChoiceNode, UniqueNode)):
            return self._cap(
                sum(self._combinations(v, visiting) for v in node.variants)
            )
        if isinstance(node, ShuffleNode):
            per_variant = 1
            for variant in node.variants:
                per_variant = self._cap(per_variant * self._combinations(variant, visiting))
            return self._cap(math.factorial(len(node.variants)) * per_variant)
        if isinstance(node, OptionalNode):
            return self._cap(self._combinations(node.content, visiting) + 1)
        if isinstance(node, MacroDefinitionNode):
            return 1  # emits nothing at its definition site
        if isinstance(node, MacroCallNode):
            definition = self._macros.get(node.name)
            if definition is None or node.name in visiting:
                return 1
            if node.name not in self._memo:
                visiting.add(node.name)
                self._memo[node.name] = self._combinations(definition.body, visiting)
                visiting.discard(node.name)
            return self._memo[node.name]
        if isinstance(node, RandomNode):
            low, high = static_text(node.low), static_text(node.high)
            try:
                if low is not None and high is not None:
                    return self._cap(max(int(high.strip()) - int(low.strip()) + 1, 1))
            except ValueError:
                pass
            return self._cap(COMBINATION_CAP)
        if isinstance(node, (RandomFloatNode, UUIDNode)):
            return self._cap(COMBINATION_CAP)
        if isinstance(node, PickNode):
            text = static_text(node.items)
            if text is not None:
                items = [item for item in text.split(",") if item.strip()]
                return max(len(items), 1)
            return self._cap(COMBINATION_CAP)
        if isinstance(node, FunctionCallNode):
            return 1  # unknowable; assume deterministic
        if isinstance(node, (TextNode, DateNode)):
            return 1
        # Variables, escapes, timestamps, ...: one outcome per generation.
        return 1
