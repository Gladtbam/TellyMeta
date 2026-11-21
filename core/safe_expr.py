"""基于标准库 AST 的白名单安全表达式求值器。"""

import ast
import base64
import json
import operator
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx2

# 表达式允许的最大字符长度与 AST 最大嵌套深度，防止极端输入引发递归或资源耗尽
_MAX_EXPR_LENGTH = 4096
_MAX_AST_DEPTH = 64
_MAX_POW_EXPONENT = 1000
_MAX_REPEAT_LEN = 1_000_000

# 表达式中允许引用的顶层名称白名单
ALLOWED_CONTEXT_NAMES: frozenset[str] = frozenset(
    {
        "response",
        "r",
        "json",
        "base64",
        "re",
        "len",
        "int",
        "str",
        "bool",
        "list",
        "dict",
    }
)

# 固定绑定的标准库模块与安全内置转换函数（不允许被外部上下文覆盖）
_SAFE_GLOBALS: dict[str, Any] = {
    "json": json,
    "base64": base64,
    "re": re,
    "len": len,
    "int": int,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# 允许直接调用的内置函数名白名单
_SAFE_CALLABLES: dict[str, Callable[..., Any]] = {
    "len": len,
    "int": int,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# 响应对象允许读取的安全只读属性
_RESPONSE_ALLOWED_ATTRS: frozenset[str] = frozenset(
    {
        "status_code",
        "text",
        "content",
        "is_success",
        "is_redirect",
        "is_error",
        "is_client_error",
        "is_server_error",
        "headers",
        "encoding",
        "reason_phrase",
        "ok",
    }
)

# re 模块允许读取的正则标志位常量
_RE_ALLOWED_ATTRS: frozenset[str] = frozenset(
    {"IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "ASCII", "A"}
)

# re.Match 对象允许读取的安全只读属性
_MATCH_ALLOWED_ATTRS: frozenset[str] = frozenset(
    {"string", "pos", "endpos", "lastindex", "lastgroup"}
)

# 按接收者类型划分的安全方法白名单（严格排除 str.format / str.format_map 与任何修改/IO 方法）
_JSON_ALLOWED_METHODS: frozenset[str] = frozenset({"loads", "dumps"})
_BASE64_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"b64decode", "b64encode", "urlsafe_b64decode", "urlsafe_b64encode"}
)
_RE_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"search", "match", "fullmatch", "findall", "split", "sub"}
)
_RESPONSE_ALLOWED_METHODS: frozenset[str] = frozenset({"json"})
_MAPPING_ALLOWED_METHODS: frozenset[str] = frozenset({"get", "keys", "values", "items"})
_STR_ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "title",
        "casefold",
        "startswith",
        "endswith",
        "split",
        "rsplit",
        "splitlines",
        "join",
        "replace",
        "find",
        "rfind",
        "index",
        "count",
        "isdigit",
        "isalpha",
        "isalnum",
        "isspace",
        "encode",
    }
)
_BYTES_ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "decode",
        "strip",
        "lstrip",
        "rstrip",
        "startswith",
        "endswith",
        "split",
        "rsplit",
        "splitlines",
        "replace",
        "find",
        "count",
    }
)
_SEQ_ALLOWED_METHODS: frozenset[str] = frozenset({"count", "index"})
_SET_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"issubset", "issuperset", "isdisjoint", "union", "intersection", "difference"}
)
_MATCH_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"group", "groups", "groupdict", "start", "end", "span"}
)

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
    ast.Invert: operator.invert,
}

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_COMPARE_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}


class _SafeExprEvaluator:
    """受限 AST 表达式求值器，仅允许白名单节点、只读属性与纯内存函数调用。"""

    def __init__(self, env: dict[str, Any], response_ids: set[int]) -> None:
        self._env = env
        self._response_ids = response_ids

    def evaluate(self, node: ast.AST, depth: int = 0) -> Any:
        """递归求值单个 AST 节点并强制深度与构造白名单校验。"""
        if depth > _MAX_AST_DEPTH:
            raise ValueError("表达式嵌套层级过深")

        if isinstance(node, ast.Expression):
            return self.evaluate(node.body, depth + 1)

        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (bool, int, float, str, bytes)) and (
                node.value is not None
            ):
                raise ValueError(f"不支持的常量类型: {type(node.value).__name__}")
            return node.value

        if isinstance(node, ast.Name):
            if node.id not in ALLOWED_CONTEXT_NAMES or node.id not in self._env:
                raise NameError(f"未定义或不允许的变量名: {node.id}")
            return self._env[node.id]

        if isinstance(node, ast.Attribute):
            return self._eval_attribute(node, depth)

        if isinstance(node, ast.Call):
            return self._eval_call(node, depth)

        if isinstance(node, ast.Subscript):
            target = self.evaluate(node.value, depth + 1)
            key = self.evaluate(node.slice, depth + 1)
            return target[key]

        if isinstance(node, ast.Slice):
            lower = (
                self.evaluate(node.lower, depth + 1) if node.lower is not None else None
            )
            upper = (
                self.evaluate(node.upper, depth + 1) if node.upper is not None else None
            )
            step = (
                self.evaluate(node.step, depth + 1) if node.step is not None else None
            )
            return slice(lower, upper, step)

        if isinstance(node, ast.List):
            return [self.evaluate(elt, depth + 1) for elt in node.elts]

        if isinstance(node, ast.Tuple):
            return tuple(self.evaluate(elt, depth + 1) for elt in node.elts)

        if isinstance(node, ast.Set):
            return {self.evaluate(elt, depth + 1) for elt in node.elts}

        if isinstance(node, ast.Dict):
            result: dict[Any, Any] = {}
            for key_node, val_node in zip(node.keys, node.values, strict=True):
                if key_node is None:
                    raise ValueError("不支持字典解包语法")
                result[self.evaluate(key_node, depth + 1)] = self.evaluate(
                    val_node, depth + 1
                )
            return result

        if isinstance(node, ast.BoolOp):
            return self._eval_bool_op(node, depth)

        if isinstance(node, ast.UnaryOp):
            op_fn = _UNARY_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")
            return op_fn(self.evaluate(node.operand, depth + 1))

        if isinstance(node, ast.BinOp):
            return self._eval_bin_op(node, depth)

        if isinstance(node, ast.Compare):
            return self._eval_compare(node, depth)

        if isinstance(node, ast.IfExp):
            cond = self.evaluate(node.test, depth + 1)
            branch = node.body if cond else node.orelse
            return self.evaluate(branch, depth + 1)

        raise ValueError(f"不支持的表达式语法节点: {type(node).__name__}")

    def _is_response_object(self, obj: Any) -> bool:
        """判断对象是否为外部 HTTP 响应实例或测试替身。"""
        return isinstance(obj, httpx2.Response) or id(obj) in self._response_ids

    def _eval_attribute(self, node: ast.Attribute, depth: int) -> Any:
        """按白名单求值只读属性访问，严禁任何下划线私有属性或非授权成员。"""
        attr = node.attr
        if attr.startswith("_"):
            raise AttributeError(f"禁止访问私有或内置属性: {attr}")

        target = self.evaluate(node.value, depth + 1)
        if self._is_response_object(target):
            if attr not in _RESPONSE_ALLOWED_ATTRS:
                raise AttributeError(f"禁止访问响应属性: {attr}")
            return getattr(target, attr)

        if target is re:
            if attr not in _RE_ALLOWED_ATTRS:
                raise AttributeError(f"禁止访问 re 模块属性: {attr}")
            return getattr(re, attr)

        if isinstance(target, re.Match):
            if attr not in _MATCH_ALLOWED_ATTRS:
                raise AttributeError(f"禁止访问匹配对象属性: {attr}")
            return getattr(target, attr)

        raise AttributeError(f"禁止访问类型 {type(target).__name__} 的属性: {attr}")

    def _eval_call_args(
        self, node: ast.Call, depth: int
    ) -> tuple[list[Any], dict[str, Any]]:
        """求值位置参数与关键字参数，显式禁止 *args 与 **kwargs 解包。"""
        args: list[Any] = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise ValueError("不支持 *args 展开调用")
            args.append(self.evaluate(arg, depth + 1))

        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise ValueError("不支持 **kwargs 展开调用")
            if kw.arg.startswith("_"):
                raise ValueError(f"非法的关键字参数名: {kw.arg}")
            kwargs[kw.arg] = self.evaluate(kw.value, depth + 1)

        return args, kwargs

    def _resolve_allowed_method(self, receiver: Any, method: str) -> Callable[..., Any]:
        """根据接收者类型从显式白名单解析可执行方法，阻止越权调用。"""
        if method.startswith("_"):
            raise AttributeError(f"禁止调用私有或内置方法: {method}")

        if receiver is json and method in _JSON_ALLOWED_METHODS:
            return getattr(json, method)
        if receiver is base64 and method in _BASE64_ALLOWED_METHODS:
            return getattr(base64, method)
        if receiver is re and method in _RE_ALLOWED_METHODS:
            return getattr(re, method)
        if self._is_response_object(receiver) and method in _RESPONSE_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, Mapping) and method in _MAPPING_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, str) and method in _STR_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, bytes) and method in _BYTES_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, (list, tuple)) and method in _SEQ_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, (set, frozenset)) and method in _SET_ALLOWED_METHODS:
            return getattr(receiver, method)
        if isinstance(receiver, re.Match) and method in _MATCH_ALLOWED_METHODS:
            return getattr(receiver, method)

        raise AttributeError(f"禁止在 {type(receiver).__name__} 上调用方法: {method}")

    def _eval_call(self, node: ast.Call, depth: int) -> Any:
        """执行白名单顶层函数或对象方法调用。"""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name not in ALLOWED_CONTEXT_NAMES:
                raise NameError(f"禁止调用或未定义的函数: {func_name}")
            fn = _SAFE_CALLABLES.get(func_name)
            if fn is None:
                raise TypeError(f"对象 {func_name} 不可直接作为函数调用")
            args, kwargs = self._eval_call_args(node, depth)
            return fn(*args, **kwargs)

        if isinstance(node.func, ast.Attribute):
            receiver = self.evaluate(node.func.value, depth + 1)
            method_fn = self._resolve_allowed_method(receiver, node.func.attr)
            args, kwargs = self._eval_call_args(node, depth)
            return method_fn(*args, **kwargs)

        raise TypeError("禁止动态调用非白名单表达式")

    def _eval_bool_op(self, node: ast.BoolOp, depth: int) -> Any:
        """按 Python 原生短路语义求值 and / or。"""
        if isinstance(node.op, ast.And):
            val: Any = True
            for item in node.values:
                val = self.evaluate(item, depth + 1)
                if not val:
                    return val
            return val

        if isinstance(node.op, ast.Or):
            val = False
            for item in node.values:
                val = self.evaluate(item, depth + 1)
                if val:
                    return val
            return val

        raise ValueError(f"不支持的布尔运算符: {type(node.op).__name__}")

    @staticmethod
    def _is_oversized_repeat(seq: Any, count: Any) -> bool:
        """判断序列重复乘法结果长度是否超出安全阈值。"""
        return (
            isinstance(seq, (str, bytes, list, tuple))
            and isinstance(count, int)
            and len(seq) * max(0, count) > _MAX_REPEAT_LEN
        )

    def _eval_bin_op(self, node: ast.BinOp, depth: int) -> Any:
        """求值二元算术运算，并对幂运算与序列重复设置安全上限。"""
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"不支持的二元运算符: {type(node.op).__name__}")

        left = self.evaluate(node.left, depth + 1)
        right = self.evaluate(node.right, depth + 1)

        if isinstance(node.op, ast.Pow):
            if isinstance(right, (int, float)) and abs(right) > _MAX_POW_EXPONENT:
                raise ValueError("幂运算指数超出安全上限")
        elif isinstance(node.op, ast.Mult) and (
            self._is_oversized_repeat(left, right)
            or self._is_oversized_repeat(right, left)
        ):
            raise ValueError("序列重复长度超出安全上限")

        return op_fn(left, right)

    def _eval_compare(self, node: ast.Compare, depth: int) -> bool:
        """按 Python 原生链式短路语义求值比较表达式。"""
        left = self.evaluate(node.left, depth + 1)
        for op_node, comp_node in zip(node.ops, node.comparators, strict=True):
            op_fn = _COMPARE_OPS.get(type(op_node))
            if op_fn is None:
                raise ValueError(f"不支持的比较运算符: {type(op_node).__name__}")
            right = self.evaluate(comp_node, depth + 1)
            if not op_fn(left, right):
                return False
            left = right
        return True


def evaluate_safe_expression(
    expr: str, context: Mapping[str, Any] | None = None
) -> Any:
    """使用纯 AST 白名单沙箱安全求值单行 Python 表达式。

    Args:
        expr: 待求值的 Python 表达式字符串。
        context: 可选的外部上下文映射，仅读取白名单允许的 ``response`` / ``r`` 变量；
            ``json``、``base64``、``re`` 与基础类型函数固定由标准库安全绑定。

    Returns:
        表达式的求值结果。

    Raises:
        SyntaxError: 表达式语法非法或包含多条语句时抛出。
        NameError: 引用未授权变量或函数名时抛出。
        AttributeError: 访问下划线私有属性或未授权方法/属性时抛出。
        TypeError: 调用不可调用对象或参数类型不匹配时抛出。
        ValueError: 包含受限 AST 构造或超出安全计算阈值时抛出。
    """
    if not isinstance(expr, str):
        raise TypeError("表达式必须为字符串")
    stripped = expr.strip()
    if not stripped:
        raise SyntaxError("表达式不能为空")
    if len(expr) > _MAX_EXPR_LENGTH:
        raise ValueError(f"表达式长度超过上限 ({_MAX_EXPR_LENGTH})")

    tree = ast.parse(stripped, mode="eval")

    env: dict[str, Any] = dict(_SAFE_GLOBALS)
    response_ids: set[int] = set()
    if context is not None:
        for key in ("response", "r"):
            if key in context:
                val = context[key]
                env[key] = val
                if val is not None:
                    response_ids.add(id(val))

    evaluator = _SafeExprEvaluator(env=env, response_ids=response_ids)
    return evaluator.evaluate(tree)
