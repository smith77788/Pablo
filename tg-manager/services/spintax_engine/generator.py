"""Generator: evaluates an AST into text.

The generator knows nothing about template syntax — it consumes only the AST,
a :class:`Context` and a :class:`FunctionRegistry`.  All randomness flows
through one seeded ``random.Random`` instance, which makes every construct
(including ``[uuid]``) reproducible under a fixed seed.
"""

from __future__ import annotations

import random
import uuid as uuid_module
from datetime import timedelta

from .context import EMPTY_CONTEXT, Context
from .errors import Diagnostic, ErrorCode, GenerationError, LimitExceededError, SourceSpan
from .functions import FunctionEnv, FunctionRegistry
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
    structural_key,
)


class _RunState:
    """Mutable state of a single generation run."""

    __slots__ = ("rng", "env", "variables", "unique_pools", "output_chars", "steps", "macro_depth")

    def __init__(self, rng: random.Random, env: FunctionEnv) -> None:
        self.rng = rng
        self.env = env
        self.variables: dict[str, str] = {}
        self.unique_pools: dict[tuple, list[int]] = {}
        self.output_chars = 0
        self.steps = 0
        self.macro_depth = 0


class Generator:
    """Evaluates one AST; reusable across calls and threads (no shared state)."""

    def __init__(
        self,
        root: RootNode,
        macros: dict[str, MacroDefinitionNode],
        limits: Limits = DEFAULT_LIMITS,
        registry: FunctionRegistry | None = None,
        source: str | None = None,
    ) -> None:
        self._root = root
        self._macros = macros
        self._limits = limits
        self._registry = registry or FunctionRegistry()
        self._source = source
        self._unique_keys: dict[int, tuple] = {}  # id(node) -> structural key
        self._handlers = {
            TextNode: self._eval_text,
            EscapeNode: self._eval_escape,
            SequenceNode: self._eval_sequence,
            ChoiceNode: self._eval_choice,
            WeightedChoiceNode: self._eval_weighted,
            ShuffleNode: self._eval_shuffle,
            UniqueNode: self._eval_unique,
            OptionalNode: self._eval_optional,
            AssignmentNode: self._eval_assignment,
            VariableNode: self._eval_variable,
            ExternalVariableNode: self._eval_external,
            MacroDefinitionNode: self._eval_macro_definition,
            MacroCallNode: self._eval_macro_call,
            RandomNode: self._eval_random,
            RandomFloatNode: self._eval_random_float,
            PickNode: self._eval_pick,
            DateNode: self._eval_date,
            TimeNode: self._eval_time,
            DateTimeNode: self._eval_datetime,
            UUIDNode: self._eval_uuid,
            TimestampNode: self._eval_timestamp,
            UnixNode: self._eval_unix,
            FunctionCallNode: self._eval_function_call,
        }

    # --- public ------------------------------------------------------------

    def generate(self, context: Context | None = None, rng: random.Random | None = None) -> str:
        context = context or EMPTY_CONTEXT
        if rng is None:
            rng = random.Random(context.seed) if context.seed is not None else random.Random()
        env = FunctionEnv(rng=rng, context=context, now=context.current_datetime())
        state = _RunState(rng, env)
        return self._eval(self._root.body, state)

    # --- machinery -----------------------------------------------------------

    def _eval(self, node: Node, state: _RunState) -> str:
        state.steps += 1
        if state.steps > self._limits.max_generation_steps:
            raise self._limit(ErrorCode.TOO_MANY_STEPS, node.span)
        return self._handlers[type(node)](node, state)

    def _limit(self, code: ErrorCode, span: SourceSpan) -> LimitExceededError:
        from .errors import CODE_CATALOG

        return LimitExceededError(
            Diagnostic.error(code, CODE_CATALOG[code].title, span), self._source
        )

    def _fail(self, code: ErrorCode, message: str, span: SourceSpan) -> GenerationError:
        return GenerationError(Diagnostic.error(code, message, span), self._source)

    def _pick_variant(self, variants: tuple[Variant, ...], state: _RunState) -> Variant:
        return variants[state.rng.randrange(len(variants))]

    # --- structural nodes ------------------------------------------------------

    def _eval_text(self, node: TextNode, state: _RunState) -> str:
        return node.text

    def _eval_escape(self, node: EscapeNode, state: _RunState) -> str:
        return node.char

    def _eval_sequence(self, node: SequenceNode, state: _RunState) -> str:
        parts: list[str] = []
        for child in node.children:
            piece = self._eval(child, state)
            state.output_chars += len(piece)
            if state.output_chars > self._limits.max_output_length:
                raise self._limit(ErrorCode.OUTPUT_TOO_LARGE, child.span)
            parts.append(piece)
        return "".join(parts)

    def _eval_choice(self, node: ChoiceNode, state: _RunState) -> str:
        return self._eval(self._pick_variant(node.variants, state).content, state)

    def _eval_weighted(self, node: WeightedChoiceNode, state: _RunState) -> str:
        weights = [v.weight if v.weight is not None else 1.0 for v in node.variants]
        chosen = state.rng.choices(node.variants, weights=weights, k=1)[0]
        return self._eval(chosen.content, state)

    def _eval_shuffle(self, node: ShuffleNode, state: _RunState) -> str:
        order = list(range(len(node.variants)))
        state.rng.shuffle(order)
        return "".join(self._eval(node.variants[i].content, state) for i in order)

    def _eval_unique(self, node: UniqueNode, state: _RunState) -> str:
        # Identically written groups share one pool, so repeating '{!A|B|C}'
        # in the template never repeats an element within one generation.
        key = self._unique_keys.get(id(node))
        if key is None:
            key = structural_key(node)
            self._unique_keys[id(node)] = key
        pool = state.unique_pools.get(key)
        if not pool:
            pool = list(range(len(node.variants)))
            state.unique_pools[key] = pool
        weights = [
            node.variants[i].weight if node.variants[i].weight is not None else 1.0
            for i in pool
        ]
        index = state.rng.choices(range(len(pool)), weights=weights, k=1)[0]
        variant = node.variants[pool.pop(index)]
        return self._eval(variant.content, state)

    def _eval_optional(self, node: OptionalNode, state: _RunState) -> str:
        if state.rng.random() < node.probability:
            return self._eval(node.content, state)
        return ""

    # --- variables and macros -----------------------------------------------------

    def _eval_assignment(self, node: AssignmentNode, state: _RunState) -> str:
        state.variables[node.name] = self._eval(node.value, state)
        return ""

    def _eval_variable(self, node: VariableNode, state: _RunState) -> str:
        value = state.variables.get(node.name)
        if value is not None:
            return value
        policy = state.env.context.on_missing_variable
        if policy == "empty":
            return ""
        if policy == "keep":
            return f"${node.name}"
        raise self._fail(
            ErrorCode.MISSING_VARIABLE,
            f"Variable '${node.name}' is read before assignment",
            node.span,
        )

    def _eval_external(self, node: ExternalVariableNode, state: _RunState) -> str:
        variables = state.env.context.variables
        if node.name in variables:
            return str(variables[node.name])
        policy = state.env.context.on_missing_variable
        if policy == "empty":
            return ""
        if policy == "keep":
            return "{{" + node.name + "}}"
        raise self._fail(
            ErrorCode.MISSING_EXTERNAL_VARIABLE,
            f"External variable '{{{{{node.name}}}}}' was not provided in the Context",
            node.span,
        )

    def _eval_macro_definition(self, node: MacroDefinitionNode, state: _RunState) -> str:
        return ""

    def _eval_macro_call(self, node: MacroCallNode, state: _RunState) -> str:
        definition = self._macros.get(node.name)
        if definition is None:
            raise self._fail(
                ErrorCode.UNDEFINED_MACRO,
                f"Macro '#{node.name}' is never defined",
                node.span,
            )
        state.macro_depth += 1
        if state.macro_depth > self._limits.max_macro_depth:
            raise self._limit(ErrorCode.MACRO_TOO_DEEP, node.span)
        try:
            return self._eval(definition.body, state)
        finally:
            state.macro_depth -= 1

    # --- built-in value nodes ----------------------------------------------------

    def _number_arg(
        self, sequence: SequenceNode, state: _RunState, caster, what: str
    ) -> float:
        text = self._eval(sequence, state).strip()
        try:
            return caster(text)
        except ValueError:
            raise self._fail(
                ErrorCode.INVALID_FUNCTION_ARG,
                f"{what}: '{text}' is not a number",
                sequence.span,
            ) from None

    def _eval_random(self, node: RandomNode, state: _RunState) -> str:
        low = int(self._number_arg(node.low, state, int, "[rand]"))
        high = int(self._number_arg(node.high, state, int, "[rand]"))
        if low > high:
            raise self._fail(
                ErrorCode.INVALID_RANDOM_RANGE,
                f"[rand] lower bound {low} exceeds upper bound {high}",
                node.span,
            )
        return str(state.rng.randint(low, high))

    def _eval_random_float(self, node: RandomFloatNode, state: _RunState) -> str:
        low = self._number_arg(node.low, state, float, "[randf]")
        high = self._number_arg(node.high, state, float, "[randf]")
        if low > high:
            raise self._fail(
                ErrorCode.INVALID_RANDOM_RANGE,
                f"[randf] lower bound {low:g} exceeds upper bound {high:g}",
                node.span,
            )
        value = state.rng.uniform(low, high)
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"

    def _eval_pick(self, node: PickNode, state: _RunState) -> str:
        raw = self._eval(node.items, state)
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if not items:
            raise self._fail(
                ErrorCode.INVALID_FUNCTION_ARG,
                "[pick] needs at least one non-empty item",
                node.span,
            )
        return items[state.rng.randrange(len(items))]

    def _eval_date(self, node: DateNode, state: _RunState) -> str:
        moment = state.env.now
        if node.offset is not None:
            days = int(self._number_arg(node.offset, state, int, "[date]"))
            moment = moment + timedelta(days=days)
        return moment.strftime(state.env.context.date_format)

    def _eval_time(self, node: TimeNode, state: _RunState) -> str:
        return state.env.now.strftime(state.env.context.time_format)

    def _eval_datetime(self, node: DateTimeNode, state: _RunState) -> str:
        return state.env.now.strftime(state.env.context.datetime_format)

    def _eval_uuid(self, node: UUIDNode, state: _RunState) -> str:
        return str(uuid_module.UUID(int=state.rng.getrandbits(128), version=4))

    def _eval_timestamp(self, node: TimestampNode, state: _RunState) -> str:
        return state.env.now.isoformat()

    def _eval_unix(self, node: UnixNode, state: _RunState) -> str:
        return str(int(state.env.now.timestamp()))

    def _eval_function_call(self, node: FunctionCallNode, state: _RunState) -> str:
        registry = state.env.context.functions or self._registry
        spec = registry.get(node.name)
        if spec is None and registry is not self._registry:
            spec = self._registry.get(node.name)
        if spec is None:
            raise self._fail(
                ErrorCode.UNKNOWN_FUNCTION,
                f"Unknown function '[{node.name}]'",
                node.span,
            )
        if not spec.accepts(len(node.args)):
            raise self._fail(
                ErrorCode.BAD_FUNCTION_ARITY,
                f"[{node.name}] expects {spec.arity_text()} argument(s), "
                f"got {len(node.args)}",
                node.span,
            )
        args = [self._eval(arg, state) for arg in node.args]
        try:
            return str(spec.handler(state.env, args))
        except (GenerationError, LimitExceededError):
            raise
        except Exception as exc:
            raise self._fail(
                ErrorCode.FUNCTION_FAILED,
                f"[{node.name}] raised {type(exc).__name__}: {exc}",
                node.span,
            ) from exc
