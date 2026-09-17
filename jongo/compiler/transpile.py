"""Compile Python functions to JavaScript.

The output leans on small helpers from ``pyrt.js`` (``$t`` truthiness, ``$eq``
equality, ``$m`` method calls, ``$call`` keyword arguments...) so compiled code
behaves like Python: empty lists are falsy, ``[1] + [2]`` concatenates,
``xs[-1]`` works, ``", ".join(names)`` works.

Free names are resolved against the live Python function (its globals and
closure), so the compiler knows whether ``add_todo`` is a server function, a
component, a helper or a constant.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap

from ..errors import CompileError
from .scope import analyse_function, is_plain_target, target_names

JS_RESERVED = frozenset(
    """arguments await break case catch class const continue debugger default delete do else enum
    eval export extends false finally for function if implements import in instanceof interface let
    new null package private protected public return static super switch this throw true try typeof
    undefined var void while with yield NaN Infinity globalThis""".split()
)

BUILTINS = frozenset(
    """len str int float bool list dict set frozenset tuple range enumerate zip sorted reversed sum min max abs
    round any all map filter print isinstance repr chr ord divmod pow bin oct hex hasattr getattr setattr callable
    Exception BaseException ValueError TypeError KeyError IndexError AttributeError RuntimeError
    AssertionError ZeroDivisionError NotImplementedError""".split()
)

UNSUPPORTED_HINTS = {
    "Import": "import at the top of the module; the compiler resolves imported names for you",
    "ImportFrom": "import at the top of the module; the compiler resolves imported names for you",
    "ClassDef": "classes don't run in the browser yet; use dicts or move the logic into a @server function",
    "With": "`with` blocks don't run in the browser; move that code into a @server function",
    "AsyncWith": "`async with` doesn't run in the browser",
    "AsyncFor": "`async for` doesn't run in the browser",
    "Match": "use if/elif chains in browser code",
    "TryStar": "use a regular try/except",
}


def js_ident(name: str) -> str:
    return name + "$" if name in JS_RESERVED else name


def js_string(value: str) -> str:
    return json.dumps(value)


def _is_str(node) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _is_num(node) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_literal(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is not None and not isinstance(node.value, bytes)


def _is_none(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _has_await(node) -> bool:
    return any(isinstance(n, ast.Await) for n in ast.walk(node))


class _Scope:
    __slots__ = ("locals", "block_for")

    def __init__(self, local_names, block_for):
        self.locals = local_names
        self.block_for = block_for


class FunctionTranspiler:
    def __init__(self, fn, resolve):
        """``resolve(name, obj, fail)`` returns the JS expression for a free name."""
        self.fn = fn
        self.resolve = resolve
        try:
            lines, start = inspect.getsourcelines(fn)
        except (OSError, TypeError) as exc:
            raise CompileError(f"can't read the source code of {fn.__qualname__}: {exc}") from None
        self.filename = inspect.getsourcefile(fn) or "<unknown>"
        self.source_lines = lines
        self.first_line = start
        tree = ast.parse(textwrap.dedent("".join(lines)))
        node = tree.body[0] if tree.body else None
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise CompileError(
                f"{fn.__qualname__} must be defined with `def` to run in the browser",
                filename=self.filename,
                lineno=start,
            )
        self.node = node
        self.closure = {}
        for name, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
            try:
                self.closure[name] = cell.cell_contents
            except ValueError:  # empty cell
                pass
        self.scopes: list[_Scope] = []
        self.loops: list[str | None] = []
        self.handlers: list[str] = []
        self.out: list[str] = []
        self.depth = 0
        self.counter = 0

    # -- plumbing ------------------------------------------------------------

    def compile(self, js_name: str) -> str:
        return self.function(self.node, js_name)

    def error(self, node, message, hint=None):
        lineno = getattr(node, "lineno", None)
        source_line = None
        if lineno and lineno <= len(self.source_lines):
            source_line = self.source_lines[lineno - 1]
        raise CompileError(
            message,
            filename=self.filename,
            lineno=self.first_line + lineno - 1 if lineno else self.first_line,
            source_line=source_line,
            hint=hint,
        )

    def emit(self, line: str):
        self.out.append("  " * self.depth + line)

    def tmp(self) -> str:
        self.counter += 1
        return f"$t{self.counter}"

    def block(self, stmts):
        self.depth += 1
        for stmt in stmts:
            self.stmt(stmt)
        self.depth -= 1

    # -- functions -----------------------------------------------------------

    def function(self, node, name=None) -> str:
        args = node.args
        positional = args.posonlyargs + args.args
        # Python evaluates defaults once, where the function is defined. Non-literal
        # defaults are captured by a wrapping arrow so `lambda i=i: ...` works in JS.
        captured: list[tuple[str, str]] = []

        def default_js(default):
            code = self.expr(default)
            if isinstance(default, ast.Constant):
                return code
            name = f"$d{len(captured)}"
            captured.append((name, code))
            return name

        defaults = [default_js(d) for d in args.defaults]
        kw_defaults = [None if d is None else default_js(d) for d in args.kw_defaults]

        bindings, block_for = analyse_function(node)
        if bindings.globals:
            self.error(
                next(iter(bindings.globals.values())),
                "`global` isn't supported in browser code",
                hint="keep values that change in state()",
            )
        params = [a.arg for a in positional]
        star = args.vararg.arg if args.vararg else None
        kwonly = [a.arg for a in args.kwonlyargs]
        dstar = args.kwarg.arg if args.kwarg else None
        declared = set(params) | set(kwonly) | {n for n in (star, dstar) if n}
        local_names = (bindings.names - bindings.nonlocals) | declared
        block_names = set()
        for loop in bindings.for_nodes:
            if id(loop) in block_for:
                block_names.update(target_names(loop.target))
        hoist = sorted(local_names - declared - block_names)

        js_params = []
        first_default = len(params) - len(defaults)
        for i, param in enumerate(params):
            ident = js_ident(param)
            js_params.append(f"{ident} = {defaults[i - first_default]}" if i >= first_default else ident)
        if star:
            js_params.append(f"{js_ident(star)} = []")
        for param, default in zip(kwonly, kw_defaults):
            js_params.append(f"{js_ident(param)} = {default}" if default is not None else js_ident(param))
        if dstar:
            js_params.append(f"{js_ident(dstar)} = {{}}")

        saved = (self.out, self.depth, self.loops, self.handlers)
        self.out, self.depth, self.loops, self.handlers = [], 1, [], []
        self.scopes.append(_Scope(local_names, block_for))
        try:
            if hoist:
                self.emit("let " + ", ".join(js_ident(n) for n in hoist) + ";")
            body = node.body if isinstance(node.body, list) else [node.body]
            if body and isinstance(body[0], ast.Expr) and _is_str(body[0].value):
                body = body[1:]
            for stmt in body:
                self.stmt(stmt)
            body_js = "\n".join(self.out)
        finally:
            self.scopes.pop()
            self.out, self.depth, self.loops, self.handlers = saved

        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        fname = js_ident(name) if name else ""
        head = f"{prefix}function {fname}({', '.join(js_params)}) {{\n{body_js}\n}}"
        sig = [json.dumps(params)]
        if star or kwonly or dstar:
            sig += [json.dumps(star), json.dumps(kwonly), json.dumps(dstar)]
        result = f"$fn({head}, {', '.join(sig)})"
        if captured:
            names = ", ".join(n for n, _ in captured)
            values = ", ".join(code for _, code in captured)
            result = f"(({names}) => {result})({values})"
        return result

    # -- statements ----------------------------------------------------------

    def stmt(self, node):
        handler = getattr(self, "s_" + type(node).__name__, None)
        if handler is None:
            kind = type(node).__name__
            self.error(node, f"`{kind}` statements aren't supported in browser code", UNSUPPORTED_HINTS.get(kind))
        handler(node)

    def s_Expr(self, node):
        if _is_str(node.value):
            return
        code = self.expr(node.value)
        if code.startswith(("{", "function")):
            code = f"({code})"
        self.emit(code + ";")

    def s_Pass(self, node):
        pass

    def s_Nonlocal(self, node):
        pass

    def s_Global(self, node):
        self.error(node, "`global` isn't supported in browser code", hint="keep values that change in state()")

    def s_Assign(self, node):
        value = self.expr(node.value)
        if len(node.targets) == 1:
            self.assign(node.targets[0], value)
            return
        t = self.tmp()
        self.emit(f"const {t} = {value};")
        for target in node.targets:
            self.assign(target, t)

    def s_AnnAssign(self, node):
        if node.value is not None:
            self.assign(node.target, self.expr(node.value))

    def assign(self, target, value: str):
        if isinstance(target, ast.Name):
            self.emit(f"{js_ident(target.id)} = {value};")
        elif isinstance(target, ast.Attribute):
            obj = self.expr(target.value)
            if self._dollar_attr(target.attr):
                self.emit(f"$sa({obj}, {js_string(target.attr)}, {value});")
            else:
                self.emit(f"{obj}.{target.attr} = {value};")
        elif isinstance(target, ast.Subscript):
            if isinstance(target.slice, ast.Slice):
                self.error(target, "slice assignment isn't supported in browser code")
            self.emit(f"$si({self.expr(target.value)}, {self.expr(target.slice)}, {value});")
        elif isinstance(target, (ast.Tuple, ast.List)):
            star = next((i for i, e in enumerate(target.elts) if isinstance(e, ast.Starred)), -1)
            t = self.tmp()
            self.emit(f"const {t} = $unpack({value}, {len(target.elts)}, {star});")
            for i, elt in enumerate(target.elts):
                self.assign(elt.value if isinstance(elt, ast.Starred) else elt, f"{t}[{i}]")
        else:
            self.error(target, f"can't assign to {type(target).__name__} in browser code")

    def s_AugAssign(self, node):
        target, rhs = node.target, self.expr(node.value)
        if isinstance(target, ast.Name):
            name = js_ident(target.id)
            self.emit(f"{name} = {self.binop(node.op, name, rhs, target, node.value, inplace=True)};")
        elif isinstance(target, ast.Attribute):
            obj = self.tmp()
            self.emit(f"const {obj} = {self.expr(target.value)};")
            if self._dollar_attr(target.attr):
                attr = js_string(target.attr)
                current = f"$ga({obj}, {attr})"
                self.emit(f"$sa({obj}, {attr}, {self.binop(node.op, current, rhs, None, node.value, True)});")
            else:
                current = f"{obj}.{target.attr}"
                self.emit(f"{current} = {self.binop(node.op, current, rhs, None, node.value, True)};")
        elif isinstance(target, ast.Subscript):
            obj, key = self.tmp(), self.tmp()
            self.emit(f"const {obj} = {self.expr(target.value)}, {key} = {self.expr(target.slice)};")
            current = f"$gi({obj}, {key})"
            self.emit(f"$si({obj}, {key}, {self.binop(node.op, current, rhs, None, node.value, True)});")
        else:
            self.error(target, "unsupported augmented assignment target")

    def s_If(self, node):
        self.emit(f"if ({self.test(node.test)}) {{")
        self.block(node.body)
        orelse = node.orelse
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            branch = orelse[0]
            self.emit(f"}} else if ({self.test(branch.test)}) {{")
            self.block(branch.body)
            orelse = branch.orelse
        if orelse:
            self.emit("} else {")
            self.block(orelse)
        self.emit("}")

    def _loop(self, header, node, body_prefix=None):
        flag = self.tmp() if node.orelse else None
        if flag:
            self.emit(f"let {flag} = false;")
        self.emit(header)
        self.loops.append(flag)
        if body_prefix:
            self.depth += 1
            body_prefix()
            self.depth -= 1
        self.block(node.body)
        self.loops.pop()
        self.emit("}")
        if flag:
            self.emit(f"if (!{flag}) {{")
            self.block(node.orelse)
            self.emit("}")

    def s_While(self, node):
        self._loop(f"while ({self.test(node.test)}) {{", node)

    def s_For(self, node):
        iterable = f"$iter({self.expr(node.iter)})"
        if id(node) in self.scopes[-1].block_for:
            self._loop(f"for (const {self.pattern(node.target)} of {iterable}) {{", node)
        elif is_plain_target(node.target):
            self._loop(f"for ({self.pattern(node.target)} of {iterable}) {{", node)
        else:
            item = self.tmp()
            self._loop(f"for (const {item} of {iterable}) {{", node, lambda: self.assign(node.target, item))

    def s_Break(self, node):
        if self.loops and self.loops[-1]:
            self.emit(f"{self.loops[-1]} = true;")
        self.emit("break;")

    def s_Continue(self, node):
        self.emit("continue;")

    def s_Return(self, node):
        self.emit(f"return {self.expr(node.value) if node.value is not None else 'null'};")

    def s_FunctionDef(self, node):
        if node.decorator_list:
            self.error(node, "decorators on nested functions aren't supported in browser code")
        self.emit(f"{js_ident(node.name)} = {self.function(node, node.name)};")

    s_AsyncFunctionDef = s_FunctionDef

    def s_Try(self, node):
        if node.finalbody:
            self.emit("try {")
            self.depth += 1
        if node.handlers:
            ok = self.tmp() if node.orelse else None
            if ok:
                self.emit(f"let {ok} = false;")
            self.emit("try {")
            self.block(node.body)
            if ok:
                self.emit(f"  {ok} = true;")
            exc = self.tmp()
            self.emit(f"}} catch ({exc}) {{")
            self.depth += 1
            first, conditional = True, True
            for handler in node.handlers:
                if handler.type is None:
                    cond = None
                else:
                    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                    cond = " || ".join(f"$isexc({exc}, {self.expr(t)})" for t in types)
                if cond is None:
                    self.emit("{" if first else "} else {")
                else:
                    self.emit(f"if ({cond}) {{" if first else f"}} else if ({cond}) {{")
                self.depth += 1
                if handler.name:
                    self.emit(f"{js_ident(handler.name)} = {exc};")
                self.handlers.append(exc)
                for stmt in handler.body:
                    self.stmt(stmt)
                self.handlers.pop()
                self.depth -= 1
                first = False
                conditional = cond is not None
                if not conditional:
                    break
            if conditional:
                self.emit("} else {")
                self.emit(f"  throw {exc};")
            self.emit("}")
            self.depth -= 1
            self.emit("}")
            if ok:
                self.emit(f"if ({ok}) {{")
                self.block(node.orelse)
                self.emit("}")
        else:
            for stmt in node.body:
                self.stmt(stmt)
        if node.finalbody:
            self.depth -= 1
            self.emit("} finally {")
            self.block(node.finalbody)
            self.emit("}")

    def s_Raise(self, node):
        if node.exc is None:
            if not self.handlers:
                self.error(node, "bare `raise` outside an except block")
            self.emit(f"throw {self.handlers[-1]};")
        else:
            self.emit(f"throw $raise({self.expr(node.exc)});")

    def s_Assert(self, node):
        message = self.expr(node.msg) if node.msg is not None else '""'
        self.emit(f"if (!{self.test(node.test)}) throw $b.AssertionError({message});")

    def s_Delete(self, node):
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self.emit(f"$del({self.expr(target.value)}, {self.expr(target.slice)});")
            elif isinstance(target, ast.Attribute):
                self.emit(f"delete {self.expr(target.value)}[{js_string(target.attr)}];")
            elif isinstance(target, ast.Name):
                self.emit(f"{js_ident(target.id)} = undefined;")
            else:
                self.error(target, "unsupported del target")

    # -- expressions ---------------------------------------------------------

    def expr(self, node) -> str:
        handler = getattr(self, "x_" + type(node).__name__, None)
        if handler is None:
            self.error(node, f"`{type(node).__name__}` expressions aren't supported in browser code")
        return handler(node)

    def test(self, node) -> str:
        """Compile ``node`` where only its truthiness matters."""
        if isinstance(node, ast.BoolOp):
            joiner = " && " if isinstance(node.op, ast.And) else " || "
            return "(" + joiner.join(self.test(v) for v in node.values) + ")"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return f"!{self.test(node.operand)}"
        if isinstance(node, ast.Compare):
            return self.x_Compare(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return "true" if node.value else "false"
        return f"$t({self.expr(node)})"

    def x_Constant(self, node):
        value = node.value
        if value is None or value is Ellipsis:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return js_string(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value != value:
                return "NaN"
            if value in (float("inf"), float("-inf")):
                return "Infinity" if value > 0 else "(-Infinity)"
            return repr(value)
        self.error(node, f"{type(value).__name__} literals aren't supported in browser code")

    def x_Name(self, node):
        name = node.id
        for scope in reversed(self.scopes):
            if name in scope.locals:
                return js_ident(name)

        def fail(message, hint=None):
            self.error(node, message, hint)

        if name in self.closure:
            return self.resolve(name, self.closure[name], fail)
        if name in self.fn.__globals__:
            return self.resolve(name, self.fn.__globals__[name], fail)
        if name in BUILTINS:
            return f"$b.{name}"
        import builtins

        if hasattr(builtins, name):
            fail(f"the builtin {name}() isn't available in browser code")
        fail(f"name {name!r} is not defined")

    @staticmethod
    def _dollar_attr(attr: str) -> bool:
        return not attr.startswith("_") and "_" in attr

    def x_Attribute(self, node):
        obj = self.expr(node.value)
        if self._dollar_attr(node.attr):
            return f"$ga({obj}, {js_string(node.attr)})"
        return f"{obj}.{node.attr}"

    def call_args(self, node):
        args = [
            f"...$iter({self.expr(a.value)})" if isinstance(a, ast.Starred) else self.expr(a)
            for a in node.args
        ]
        if not node.keywords:
            return args, None
        items = [
            f"...{self.expr(k.value)}" if k.arg is None else f"{js_string(k.arg)}: {self.expr(k.value)}"
            for k in node.keywords
        ]
        return args, "{" + ", ".join(items) + "}"

    def x_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "super":
            self.error(node, "super() isn't supported in browser code")
        args, kw = self.call_args(node)
        if isinstance(func, ast.Attribute):
            obj = self.expr(func.value)
            tail = f", {kw}" if kw else ""
            return f"$m({obj}, {js_string(func.attr)}, [{', '.join(args)}]{tail})"
        callee = self.expr(func)
        if kw:
            return f"$call({callee}, [{', '.join(args)}], {kw})"
        return f"{callee}({', '.join(args)})"

    def binop(self, op, left, right, left_node=None, right_node=None, inplace=False) -> str:
        kind = type(op)
        if kind is ast.Add:
            if inplace:
                return f"$iadd({left}, {right})"
            if _is_str(left_node) or _is_str(right_node) or (_is_num(left_node) and _is_num(right_node)):
                return f"({left} + {right})"
            return f"$add({left}, {right})"
        if kind is ast.Mult:
            if _is_num(left_node) and _is_num(right_node):
                return f"({left} * {right})"
            return f"$mul({left}, {right})"
        if kind is ast.Sub:
            if _is_num(left_node) and _is_num(right_node):
                return f"({left} - {right})"
            return f"$sub({left}, {right})"  # set difference when both are sets
        simple = {
            ast.Div: "/", ast.Pow: "**",
        }
        if kind in simple:
            return f"({left} {simple[kind]} {right})"
        helpers = {
            ast.FloorDiv: "$floordiv", ast.Mod: "$mod", ast.BitOr: "$bor",
            ast.BitAnd: "$band", ast.BitXor: "$bxor",  # set intersection / symmetric difference
            ast.LShift: "$lshift", ast.RShift: "$rshift",  # BigInt-backed, not 32-bit JS shifts
        }
        if kind in helpers:
            return f"{helpers[kind]}({left}, {right})"
        self.error(left_node or right_node, f"operator {kind.__name__} isn't supported in browser code")

    def x_BinOp(self, node):
        return self.binop(node.op, self.expr(node.left), self.expr(node.right), node.left, node.right)

    def x_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return f"!{self.test(node.operand)}"
        symbol = {ast.USub: "-", ast.UAdd: "+", ast.Invert: "~"}[type(node.op)]
        return f"({symbol}{self.expr(node.operand)})"

    def x_BoolOp(self, node):
        helper = "$and" if isinstance(node.op, ast.And) else "$or"
        is_async = any(_has_await(v) for v in node.values[1:])
        arrow = "async () => " if is_async else "() => "
        result = self.expr(node.values[-1])
        for value in reversed(node.values[:-1]):
            result = f"{helper}({self.expr(value)}, {arrow}{result})"
        return f"(await {result})" if is_async else result

    def x_Compare(self, node):
        parts = []
        left_node, left = node.left, self.expr(node.left)
        for op, right_node in zip(node.ops, node.comparators):
            right = self.expr(right_node)
            parts.append(self.compare(op, left_node, left, right_node, right))
            left_node, left = right_node, right
        return parts[0] if len(parts) == 1 else "(" + " && ".join(parts) + ")"

    def compare(self, op, left_node, left, right_node, right) -> str:
        kind = type(op)
        if kind in (ast.Eq, ast.NotEq, ast.Is, ast.IsNot):
            positive = kind in (ast.Eq, ast.Is)
            if _is_none(right_node) or _is_none(left_node):
                other = left if _is_none(right_node) else right
                return f"({other} {'==' if positive else '!='} null)"
            if kind in (ast.Is, ast.IsNot):
                return f"({left} {'===' if positive else '!=='} {right})"
            # Always route == / != through $eq: JS === is wrong for Python value equality
            # (True == 1, 1 == 1.0, list/dict/set structural equality).
            return f"$eq({left}, {right})" if positive else f"!$eq({left}, {right})"
        if kind is ast.In:
            return f"$in({left}, {right})"
        if kind is ast.NotIn:
            return f"!$in({left}, {right})"
        symbol = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[kind]
        return f"({left} {symbol} {right})"

    def x_IfExp(self, node):
        return f"({self.test(node.test)} ? {self.expr(node.body)} : {self.expr(node.orelse)})"

    def _elements(self, elts) -> str:
        return ", ".join(
            f"...$iter({self.expr(e.value)})" if isinstance(e, ast.Starred) else self.expr(e) for e in elts
        )

    def x_List(self, node):
        return f"[{self._elements(node.elts)}]"

    x_Tuple = x_List

    def x_Set(self, node):
        return f"$set([{self._elements(node.elts)}])"  # normalises bool/int members (True == 1)

    def x_Dict(self, node):
        items = []
        for key, value in zip(node.keys, node.values):
            if key is None:
                items.append(f"...{self.expr(value)}")
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                items.append(f"{js_string(key.value)}: {self.expr(value)}")
            else:
                items.append(f"[$key({self.expr(key)})]: {self.expr(value)}")  # True/False key == 1/0
        return "({" + ", ".join(items) + "})"

    def comprehension(self, node, kind) -> str:
        generators = node.generators
        first_iter = self.expr(generators[0].iter)
        names = set()
        for gen in generators:
            if gen.is_async:
                self.error(node, "async comprehensions aren't supported in browser code")
            if not is_plain_target(gen.target):
                self.error(gen.target, "comprehension variables must be plain names in browser code")
            names.update(target_names(gen.target))
        self.scopes.append(_Scope(names, set()))
        try:
            init = {"list": "[]", "set": "new Set()", "dict": "{}"}[kind]
            code = [f"const $r = {init};"]
            for i, gen in enumerate(generators):
                iterable = "$i" if i == 0 else self.expr(gen.iter)
                code.append(f"for (const {self.pattern(gen.target)} of $iter({iterable})) {{")
                for cond in gen.ifs:
                    code.append(f"if (!{self.test(cond)}) continue;")
            if kind == "dict":
                code.append(f"$r[$key({self.expr(node.key)})] = {self.expr(node.value)};")
            elif kind == "set":
                code.append(f"$r.add($key({self.expr(node.elt)}));")
            else:
                code.append(f"$r.push({self.expr(node.elt)});")
            code.append("}" * len(generators))
            code.append("return $r;")
        finally:
            self.scopes.pop()
        is_async = _has_await(node)
        call = f"({'async ' if is_async else ''}($i) => {{ {' '.join(code)} }})({first_iter})"
        return f"(await {call})" if is_async else call

    def x_ListComp(self, node):
        return self.comprehension(node, "list")

    x_GeneratorExp = x_ListComp

    def x_SetComp(self, node):
        return self.comprehension(node, "set")

    def x_DictComp(self, node):
        return self.comprehension(node, "dict")

    def pattern(self, target) -> str:
        if isinstance(target, ast.Name):
            return js_ident(target.id)
        if isinstance(target, (ast.Tuple, ast.List)):
            return "[" + ", ".join(self.pattern(e) for e in target.elts) + "]"
        if isinstance(target, ast.Starred):
            return "..." + self.pattern(target.value)
        self.error(target, "unsupported loop target in browser code")

    def x_JoinedStr(self, node):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${"))
            elif isinstance(value, ast.FormattedValue):
                inner = self.expr(value.value)
                if value.conversion in (ord("r"), ord("a")):
                    inner = f"$repr({inner})"
                if value.format_spec is not None:
                    parts.append(f"${{$fmt({inner}, {self.expr(value.format_spec)})}}")
                else:
                    parts.append(f"${{$str({inner})}}")
        return "`" + "".join(parts) + "`"

    def x_Lambda(self, node):
        fake = ast.FunctionDef(name="", args=node.args, body=[ast.Return(node.body)], decorator_list=[])
        ast.copy_location(fake, node)
        ast.fix_missing_locations(fake)
        return self.function(fake)

    def x_Await(self, node):
        return f"(await {self.expr(node.value)})"

    def x_NamedExpr(self, node):
        return f"({js_ident(node.target.id)} = {self.expr(node.value)})"

    def x_Subscript(self, node):
        obj = self.expr(node.value)
        index = node.slice
        if isinstance(index, ast.Slice):
            bounds = [self.expr(p) if p is not None else "null" for p in (index.lower, index.upper, index.step)]
            return f"$slice({obj}, {', '.join(bounds)})"
        return f"$gi({obj}, {self.expr(index)})"


def transpile_function(fn, js_name: str, resolve) -> str:
    """Compile ``fn`` into a JS expression that evaluates to the function."""
    return FunctionTranspiler(fn, resolve).compile(js_name)
