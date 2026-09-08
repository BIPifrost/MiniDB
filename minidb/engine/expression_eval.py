"""WAITING: expression evaluation will be implemented after bound types land.

Zhang Zhen owns ``core/expressions.py`` and ``compiler/bound.py``.  The final
evaluator must consume their ``BoundExpr`` and ``ExprOp`` definitions and call
the shared ``resolve_result_type`` function instead of copying a type table.

This file is intentionally non-functional for now.  Adding a second temporary
expression hierarchy here would create an incompatible public contract.  The
current executor scaffold uses Python predicate callables only inside private
tests; that behavior must not become part of the final evaluator API.
"""
