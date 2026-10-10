"""Semantic analyzer: everything that is checkable without generating.

Runs after the parser on a structurally valid AST and produces diagnostics —
it never raises.  Checks include macro resolution, macro cycle detection,
function existence/arity and static argument validation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Diagnostic, ErrorCode
from .functions import FunctionRegistry
from .nodes import (
    AssignmentNode,
    DateNode,
    EscapeNode,
    ExternalVariableNode,
    FunctionCallNode,
    MacroCallNode,
    MacroDefinitionNode,
    Node,
    PickNode,
    RandomFloatNode,
    RandomNode,
    RootNode,
    SequenceNode,
    TextNode,
    VariableNode,
    iter_children,
)


def static_text(sequence: SequenceNode | None) -> str | None:
    """Return the literal value of a sequence if it is fully static."""
    if sequence is None:
        return None
    parts: list[str] = []
    for child in sequence.children:
        if isinstance(child, TextNode):
            parts.append(child.text)
        elif isinstance(child, EscapeNode):
            parts.append(child.char)
        else:
            return None
    return "".join(parts)


def walk(node: Node):
    """Yield every node of the subtree in depth-first order."""
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(iter_children(current)))


@dataclass(frozen=True, slots=True)
class SemanticInfo:
    """Facts about a template collected by :class:`SemanticAnalyzer`."""

    diagnostics: tuple[Diagnostic, ...]
    macros: dict[str, MacroDefinitionNode]
    variables: frozenset[str]
    external_variables: frozenset[str]
    functions: frozenset[str]

    @property
    def has_errors(self) -> bool:
        from .errors import Severity

        return any(d.severity is Severity.ERROR for d in self.diagnostics)


class SemanticAnalyzer:
    """Stateless facade: one :meth:`analyze` call per template."""

    def __init__(self, registry: FunctionRegistry | None = None) -> None:
        self._registry = registry

    def analyze(self, root: RootNode) -> SemanticInfo:
        diagnostics: list[Diagnostic] = []
        macros = self._collect_macros(root, diagnostics)
        called: set[str] = set()
        assigned: set[str] = set()
        read: dict[str, Node] = {}
        externals: set[str] = set()
        functions: set[str] = set()

        for node in walk(root):
            if isinstance(node, MacroCallNode):
                called.add(node.name)
                if node.name not in macros:
                    diagnostics.append(
                        Diagnostic.error(
                            ErrorCode.UNDEFINED_MACRO,
                            f"Macro '#{node.name}' is never defined",
                            node.span,
                        )
                    )
            elif isinstance(node, AssignmentNode):
                assigned.add(node.name)
            elif isinstance(node, VariableNode):
                read.setdefault(node.name, node)
            elif isinstance(node, ExternalVariableNode):
                externals.add(node.name)
            elif isinstance(node, FunctionCallNode):
                functions.add(node.name)
                self._check_function(node, diagnostics)
            elif isinstance(node, (RandomNode, RandomFloatNode)):
                self._check_range(node, diagnostics)
            elif isinstance(node, DateNode):
                self._check_date(node, diagnostics)
            elif isinstance(node, PickNode):
                self._check_pick(node, diagnostics)

        self._check_cycles(macros, diagnostics)
        for name, definition in macros.items():
            if name not in called:
                diagnostics.append(
                    Diagnostic.warning(
                        ErrorCode.UNUSED_MACRO,
                        f"Macro '#{name}' is defined but never used",
                        definition.span,
                    )
                )
        for name, node in read.items():
            if name not in assigned:
                diagnostics.append(
                    Diagnostic.warning(
                        ErrorCode.UNASSIGNED_VARIABLE,
                        f"Variable '${name}' is read but never assigned",
                        node.span,
                    )
                )

        return SemanticInfo(
            diagnostics=tuple(diagnostics),
            macros=macros,
            variables=frozenset(read) | frozenset(assigned),
            external_variables=frozenset(externals),
            functions=frozenset(functions),
        )

    # --- individual checks ---------------------------------------------------

    def _collect_macros(
        self, root: RootNode, diagnostics: list[Diagnostic]
    ) -> dict[str, MacroDefinitionNode]:
        macros: dict[str, MacroDefinitionNode] = {}
        for definition in root.macros:
            if definition.name in macros:
                diagnostics.append(
                    Diagnostic.error(
                        ErrorCode.MACRO_REDEFINED,
                        f"Macro '#{definition.name}' is defined more than once",
                        definition.span,
                    )
                )
            else:
                macros[definition.name] = definition
        return macros

    def _check_cycles(
        self, macros: dict[str, MacroDefinitionNode], diagnostics: list[Diagnostic]
    ) -> None:
        graph = {
            name: [
                n.name
                for n in walk(definition.body)
                if isinstance(n, MacroCallNode) and n.name in macros
            ]
            for name, definition in macros.items()
        }
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(graph, WHITE)
        reported: set[str] = set()

        def visit(name: str, path: list[str]) -> None:
            color[name] = GRAY
            path.append(name)
            for callee in graph[name]:
                if color[callee] == GRAY:
                    cycle = path[path.index(callee) :] + [callee]
                    if callee not in reported:
                        reported.update(cycle)
                        diagnostics.append(
                            Diagnostic.error(
                                ErrorCode.MACRO_CYCLE,
                                "Cyclic macro definition: "
                                + " -> ".join(f"#{n}" for n in cycle),
                                macros[callee].span,
                            )
                        )
                elif color[callee] == WHITE:
                    visit(callee, path)
            path.pop()
            color[name] = BLACK

        for name in graph:
            if color[name] == WHITE:
                visit(name, [])

    def _check_function(
        self, node: FunctionCallNode, diagnostics: list[Diagnostic]
    ) -> None:
        if self._registry is None:
            return
        spec = self._registry.get(node.name)
        if spec is None:
            diagnostics.append(
                Diagnostic.error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"Unknown function '[{node.name}]'",
                    node.span,
                )
            )
            return
        if not spec.accepts(len(node.args)):
            diagnostics.append(
                Diagnostic.error(
                    ErrorCode.BAD_FUNCTION_ARITY,
                    f"[{node.name}] expects {spec.arity_text()} argument(s), "
                    f"got {len(node.args)}",
                    node.span,
                )
            )

    def _check_range(
        self, node: RandomNode | RandomFloatNode, diagnostics: list[Diagnostic]
    ) -> None:
        is_int = isinstance(node, RandomNode)
        caster = int if is_int else float
        name = "rand" if is_int else "randf"
        bounds: list[float] = []
        for sequence in (node.low, node.high):
            text = static_text(sequence)
            if text is None:
                return  # dynamic bound: checked at generation time
            try:
                bounds.append(caster(text.strip()))
            except ValueError:
                diagnostics.append(
                    Diagnostic.error(
                        ErrorCode.INVALID_FUNCTION_ARG,
                        f"[{name}] bound '{text.strip()}' is not a number",
                        node.span,
                    )
                )
                return
        if bounds[0] > bounds[1]:
            diagnostics.append(
                Diagnostic.error(
                    ErrorCode.INVALID_RANDOM_RANGE,
                    f"[{name}] lower bound {bounds[0]:g} exceeds upper bound {bounds[1]:g}",
                    node.span,
                )
            )

    def _check_date(self, node: DateNode, diagnostics: list[Diagnostic]) -> None:
        text = static_text(node.offset)
        if node.offset is None or text is None:
            return
        try:
            int(text.strip())
        except ValueError:
            diagnostics.append(
                Diagnostic.error(
                    ErrorCode.INVALID_FUNCTION_ARG,
                    f"[date] offset '{text.strip()}' is not an integer",
                    node.span,
                )
            )

    def _check_pick(self, node: PickNode, diagnostics: list[Diagnostic]) -> None:
        text = static_text(node.items)
        if text is None:
            return
        if not [item for item in text.split(",") if item.strip()]:
            diagnostics.append(
                Diagnostic.error(
                    ErrorCode.INVALID_FUNCTION_ARG,
                    "[pick] needs at least one non-empty item",
                    node.span,
                )
            )
