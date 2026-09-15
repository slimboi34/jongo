"""Scope analysis for the Python -> JavaScript transpiler.

Python scopes are per function, JavaScript scopes are per block. We hoist every
name a Python function binds into one ``let`` at the top of the JS function, which
reproduces Python semantics exactly. The one deliberate improvement: a ``for`` loop
whose variable is only used inside the loop gets a per-iteration ``const``, so
closures created in the loop (event handlers!) capture each item instead of the last.
"""
from __future__ import annotations

import ast


def target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in target_names(elt)]
    if isinstance(target, ast.Starred):
        return target_names(target.value)
    return []


def is_plain_target(target) -> bool:
    """Only names (possibly nested in tuples/starred) - usable as a JS binding pattern."""
    if isinstance(target, ast.Name):
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        return all(is_plain_target(e) for e in target.elts)
    if isinstance(target, ast.Starred):
        return isinstance(target.value, ast.Name)
    return False


class Bindings(ast.NodeVisitor):
    """Collect the names bound directly in one function scope."""

    def __init__(self):
        self.names: set[str] = set()
        self.nonlocals: set[str] = set()
        self.globals: dict[str, ast.AST] = {}
        self.store_counts: dict[str, int] = {}
        self.for_nodes: list[ast.For] = []

    def _bind(self, name):
        self.names.add(name)
        self.store_counts[name] = self.store_counts.get(name, 0) + 1

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)

    def _visit_function(self, node):
        if not isinstance(node, ast.Lambda):
            self._bind(node.name)
            for dec in node.decorator_list:
                self.visit(dec)
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(default)

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_function

    def visit_ClassDef(self, node):
        self._bind(node.name)

    def _visit_comprehension(self, node):
        # Only walrus targets leak out of a comprehension.
        for sub in ast.walk(node):
            if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                self._bind(sub.target.id)

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_NamedExpr(self, node):
        self._bind(node.target.id)
        self.visit(node.value)

    def visit_ExceptHandler(self, node):
        if node.name:
            self._bind(node.name)
        self.generic_visit(node)

    def visit_Nonlocal(self, node):
        self.nonlocals.update(node.names)

    def visit_Global(self, node):
        for name in node.names:
            self.globals[name] = node

    def visit_For(self, node):
        self.for_nodes.append(node)
        self.generic_visit(node)


def analyse_function(node) -> tuple[Bindings, set[int]]:
    """Return the bindings of ``node`` and the ids of For nodes that can use block-scoped targets."""
    bindings = Bindings()
    body = node.body if isinstance(node.body, list) else [node.body]
    for stmt in body:
        bindings.visit(stmt)

    block_for: set[int] = set()
    if bindings.for_nodes:
        all_names = [n for stmt in body for n in ast.walk(stmt) if isinstance(n, ast.Name)]
        for loop in bindings.for_nodes:
            if not is_plain_target(loop.target):
                continue
            names = target_names(loop.target)
            if any(bindings.store_counts.get(n, 0) != 1 or n in bindings.nonlocals for n in names):
                continue
            inside = {id(n) for n in ast.walk(loop)}
            if all(id(n) in inside for n in all_names if n.id in names):
                block_for.add(id(loop))
    return bindings, block_for
