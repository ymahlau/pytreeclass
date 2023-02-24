from __future__ import annotations

import functools as ft
import hashlib
import inspect
import math
import operator as op
from typing import Any, Callable

import jax.tree_util as jtu
import numpy as np

"""A wrapper around a tree that allows to use the tree leaves as if they were scalars."""

PyTree = Any
_empty = inspect.Parameter.empty


def _hash_node(node):
    if hasattr(node, "dtype") and hasattr(node, "shape"):
        return hashlib.sha256(np.array(node).tobytes()).hexdigest()
    if isinstance(node, set):
        return hash(frozenset(node))
    if isinstance(node, dict):
        return hash(frozenset(node.items()))
    if isinstance(node, list):
        return hash(tuple(node))
    return hash(node)


def _hash(tree):
    hashed = jtu.tree_map(_hash_node, jtu.tree_leaves(tree))
    return hash((*hashed, jtu.tree_structure(tree)))


def _copy(tree: PyTree) -> PyTree:
    """Return a copy of the tree"""
    return jtu.tree_unflatten(*jtu.tree_flatten(tree)[::-1])


_non_partial = object()


class _Partial(ft.partial):
    def __call__(self, *args, **keywords):
        # https://stackoverflow.com/a/7811270
        keywords = {**self.keywords, **keywords}
        iargs = iter(args)
        args = (next(iargs) if arg is _non_partial else arg for arg in self.args)
        return self.func(*args, *iargs, **keywords)


@ft.lru_cache(maxsize=None)
def bcmap(
    func: Callable[..., Any], *, is_leaf: Callable[[Any], bool] | None = None
) -> Callable:
    """(map)s a function over pytrees leaves with automatic (b)road(c)asting for scalar arguments

    Args:
        func: the function to be mapped over the pytree
        is_leaf: a function that returns True if the argument is a leaf of the pytree

    Example:
        >>> @pytc.treeclass
        ... class Test:
        ...    a: tuple[int] = (1,2,3)
        ...    b: tuple[int] = (4,5,6)
        ...    c: jnp.ndarray = jnp.array([1,2,3])

        >>> tree = Test()
        >>> # 0 is broadcasted to all leaves of the pytree

        >>> print(pytc.bcmap(jnp.where)(tree>1, tree, 0))
        Test(a=(0,2,3), b=(4,5,6), c=[0 2 3])

        >>> print(pytc.bcmap(jnp.where)(tree>1, 0, tree))
        Test(a=(1,0,0), b=(0,0,0), c=[1 0 0])

        >>> # 1 is broadcasted to all leaves of the list pytree
        >>> bcmap(lambda x,y:x+y)([1,2,3],1)
        [2, 3, 4]

        >>> # trees are summed leaf-wise
        >>> bcmap(lambda x,y:x+y)([1,2,3],[1,2,3])
        [2, 4, 6]

        >>> # Non scalar second args case
        >>> bcmap(lambda x,y:x+y)([1,2,3],[[1,2,3],[1,2,3]])
        TypeError: unsupported operand type(s) for +: 'int' and 'list'

        >>> # using **numpy** functions on pytrees
        >>> import jax.numpy as jnp
        >>> bcmap(jnp.add)([1,2,3],[1,2,3])
        [DeviceArray(2, dtype=int32, weak_type=True),
        DeviceArray(4, dtype=int32, weak_type=True),
        DeviceArray(6, dtype=int32, weak_type=True)]
    """

    def wrapper(*args, **kwargs):
        if len(args) > 0:
            # positional arguments are passed the argument to be compare
            # the tree structure with is the first argument
            leaves0, treedef0 = jtu.tree_flatten(args[0], is_leaf=is_leaf)
            args = args[1:]
            masked_args = [_non_partial]
            masked_kwargs = {}
            leaves = [leaves0]
            leaves_keys = []

        else:
            # only kwargs are passed the argument to be compare
            # the tree structure with is the first kwarg
            key0 = next(iter(kwargs))
            leaves0, treedef0 = jtu.tree_flatten(kwargs.pop(key0), is_leaf=is_leaf)
            masked_args = []
            masked_kwargs = {key0: _non_partial}
            leaves = [leaves0]
            leaves_keys = [key0]

        for arg in args:
            if jtu.tree_structure(arg) == treedef0:
                masked_args += [_non_partial]
                leaves += [treedef0.flatten_up_to(arg)]
            else:
                masked_args += [arg]

        for key in kwargs:
            if jtu.tree_structure(kwargs[key]) == treedef0:
                masked_kwargs[key] = _non_partial
                leaves += [treedef0.flatten_up_to(kwargs[key])]
                leaves_keys += [key]
            else:
                masked_kwargs[key] = kwargs[key]

        func_ = _Partial(func, *masked_args, **masked_kwargs)

        if len(leaves_keys) == 0:
            # no kwargs leaves are present, so we can immediately zip
            return jtu.tree_unflatten(treedef0, [func_(*xs) for xs in zip(*leaves)])

        # kwargs leaves are present, so we need to zip them
        kwargnum = len(leaves) - len(leaves_keys)
        all_leaves = []
        for xs in zip(*leaves):
            xs_args, xs_kwargs = xs[:kwargnum], xs[kwargnum:]
            all_leaves += [func_(*xs_args, **dict(zip(leaves_keys, xs_kwargs)))]
        return jtu.tree_unflatten(treedef0, all_leaves)

    return wrapper


class _TreeOperator:
    """Base class for tree operators used

    Example:
        >>> import jax.tree_util as jtu
        >>> import dataclasses as dc
        >>> @jtu.register_pytree_node_class`
        ... @dc.dataclass
        ... class Tree(_TreeOperator):
        ...    a: int =1
        ...    def tree_flatten(self):
        ...        return (self.a,), None
        ...    @classmethod
        ...    def tree_unflatten(cls, _, children):
        ...        return cls(*children)

        >>> tree = Tree()
        >>> tree + 1
        Tree(a=2)
    """

    __abs__ = bcmap(op.abs)
    __add__ = bcmap(op.add)
    __and__ = bcmap(op.and_)
    __ceil__ = bcmap(math.ceil)
    __copy__ = _copy
    __divmod__ = bcmap(divmod)
    __eq__ = bcmap(op.eq)
    __floor__ = bcmap(math.floor)
    __floordiv__ = bcmap(op.floordiv)
    __ge__ = bcmap(op.ge)
    __gt__ = bcmap(op.gt)
    __inv__ = bcmap(op.inv)
    __invert__ = bcmap(op.invert)
    __le__ = bcmap(op.le)
    __lshift__ = bcmap(op.lshift)
    __lt__ = bcmap(op.lt)
    __matmul__ = bcmap(op.matmul)
    __mod__ = bcmap(op.mod)
    __mul__ = bcmap(op.mul)
    __ne__ = bcmap(op.ne)
    __neg__ = bcmap(op.neg)
    __or__ = bcmap(op.or_)
    __pos__ = bcmap(op.pos)
    __pow__ = bcmap(op.pow)
    __radd__ = bcmap(op.add)
    __rand__ = bcmap(op.and_)
    __rdivmod__ = bcmap(divmod)
    __rfloordiv__ = bcmap(op.floordiv)
    __rlshift__ = bcmap(op.lshift)
    __rmod__ = bcmap(op.mod)
    __rmul__ = bcmap(op.mul)
    __ror__ = bcmap(op.or_)
    __round__ = bcmap(round)
    __rpow__ = bcmap(op.pow)
    __rrshift__ = bcmap(op.rshift)
    __rshift__ = bcmap(op.rshift)
    __rsub__ = bcmap(op.sub)
    __rtruediv__ = bcmap(op.truediv)
    __rxor__ = bcmap(op.xor)
    __sub__ = bcmap(op.sub)
    __truediv__ = bcmap(op.truediv)
    __trunk__ = bcmap(math.trunc)
    __xor__ = bcmap(op.xor)
    __hash__ = _hash
