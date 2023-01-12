from __future__ import annotations

import dataclasses as dc
import functools as ft
import operator as op
from typing import Any

import jax.numpy as jnp
import jax.tree_util as jtu
import numpy as np
from jax.core import Tracer

from pytreeclass._src.dataclass_util import _mutable
from pytreeclass._src.tree_indexer import _at_indexer
from pytreeclass.tree_viz.tree_pprint import tree_repr, tree_str

PyTree = Any


class NonDiffField(dc.Field):
    # intended for non-differentiable fields that will
    # be excluded from the tree flattening
    pass


class FrozenField(NonDiffField):
    # intended for fields that will be excluded from the tree flattening
    # by the `tree_filter`.
    pass


def field(
    *,
    nondiff: bool = False,
    default=dc.MISSING,
    default_factory=dc.MISSING,
    init=True,
    repr=True,
    hash=None,
    compare=True,
    metadata=None,
    kw_only=dc.MISSING,
):
    """dataclass field with additional `nondiff` flag"""

    if default is not dc.MISSING and default_factory is not dc.MISSING:
        raise ValueError("cannot specify both default and default_factory")

    args = dict(
        default=default,
        default_factory=default_factory,
        init=init,
        repr=repr,
        hash=hash,
        compare=compare,
        metadata=metadata,
    )

    if "kw_only" in dir(dc.Field):
        args.update(kw_only=kw_only)

    if nondiff:
        return NonDiffField(**args)

    return dc.Field(**args)


def _setattr(tree: PyTree, key: str, value: Any) -> None:
    """set the attribute of the tree

    Args:
        tree: instance of treeclass
        key: key of the attribute
        value: value of the attribute

    Raises:
        FrozenInstanceError: if the tree is frozen

    Returns:
        None

    Note:
        This is a custom setattr function for treeclass.
        It is used to register value to the dataclass fields if the value is a dataclass.
        This is to avoid unnecessary dataclass fields declaration.

    """
    if getattr(tree.__dataclass_params__, "frozen"):
        msg = f"Cannot set {key}={value!r}. Use `.at['{key}'].set({value!r})` instead."
        raise dc.FrozenInstanceError(msg)

    object.__setattr__(tree, key, value)

    if dc.is_dataclass(value) and (key not in [f.name for f in dc.fields(tree)]):
        field_item = dc.field()
        object.__setattr__(field_item, "name", key)
        object.__setattr__(field_item, "type", type(value))
        object.__setattr__(field_item, "_field_type", dc._FIELD)

        # register it to dataclass fields
        tree.__dataclass_fields__[key] = field_item


def _delattr(tree, key: str) -> None:
    """delete the attribute of the tree

    Args:
        tree: instance of treeclass
        key: key of the attribute

    Raises:
        FrozenInstanceError: if the tree is frozen

    Returns:
        None
    """
    if getattr(tree.__dataclass_params__, "frozen"):
        raise dc.FrozenInstanceError(f"Cannot delete {key}.")
    object.__delattr__(tree, key)


def _new(cls, *a, **k) -> PyTree:
    """custom __new__ for treeclass

    Args:
        cls: class of the treeclass
        *a: arguments
        **k: keyword arguments

    Returns:
        instance of the treeclass
    """
    # create a new instance of the treeclass
    tree = object.__new__(cls)

    _params = dc._DataclassParams(
        init=tree.__dataclass_params__.init,
        repr=tree.__dataclass_params__.repr,
        eq=tree.__dataclass_params__.eq,
        order=tree.__dataclass_params__.order,
        unsafe_hash=tree.__dataclass_params__.unsafe_hash,
        frozen=tree.__dataclass_params__.frozen,
    )

    setattr(tree, "__dataclass_params__", _params)
    setattr(tree, "__dataclass_fields__", {f.name: f for f in dc.fields(tree)})

    for field_item in dc.fields(tree):
        if field_item.default is not dc.MISSING:
            setattr(tree, field_item.name, field_item.default)
    return tree


def _hash(tree):
    """Return a hash of the tree"""

    def _hash_node(node):
        """hash the leaves of the tree"""
        if isinstance(node, (jnp.ndarray, np.ndarray)):
            return np.array(node).tobytes()
        elif isinstance(node, set):
            # jtu.tree_map does not traverse sets
            return frozenset(node)
        return node

    return hash(
        (*jtu.tree_map(_hash_node, jtu.tree_leaves(tree)), jtu.tree_structure(tree))
    )


def _copy(tree: PyTree) -> PyTree:
    """Return a copy of the tree"""
    return jtu.tree_unflatten(*jtu.tree_flatten(tree)[::-1])


class _MetaDict(dict):
    # see https://github.com/google/jax/issues/13027
    __eq__ = lambda x, y: x.__dict__ == y.__dict__


def _flatten(tree) -> tuple[Any, tuple[str, _MetaDict[str, Any]]]:
    """Flatten rule for `jax.tree_flatten`"""
    # all values are marked as static by default and will be stored in the static dict
    # only the dynamic values will be stored in the dynamic dict
    # this is to avoid unnecessary dataclass fields declaration for the static values
    static, dynamic = _MetaDict(tree.__dict__), dict()

    for field_item in dc.fields(tree):
        if not isinstance(field_item, NonDiffField):
            dynamic[field_item.name] = static.pop(field_item.name)

    return dynamic.values(), (dynamic.keys(), static)


def _unflatten(cls, treedef, leaves):
    """Unflatten rule for `jax.tree_unflatten`"""
    tree = object.__new__(cls)
    # update the instance values with the retrieved dynamic and static values
    tree.__dict__.update(dict(zip(treedef[0], leaves)))
    tree.__dict__.update(treedef[1])
    return tree


def _dispatched_op_tree_map(func, lhs, rhs=None, is_leaf=None):
    """`jtu.tree_map` for unary/binary operators broadcasting"""
    # if rhs is a tree of the same type as lhs then we use the tree_map to apply the operator leaf-wise
    if isinstance(rhs, type(lhs)):
        return jtu.tree_map(func, lhs, rhs, is_leaf=is_leaf)
    # if rhs is a scalar then we use the tree_map to apply the operator with broadcasting the rhs
    elif isinstance(rhs, (Tracer, jnp.ndarray, int, float, complex, bool, str)):
        return jtu.tree_map(lambda x: func(x, rhs), lhs, is_leaf=is_leaf)
    # if rhs is None , then we apply the operator to the tree leaves (i.e. unary operation)
    elif isinstance(rhs, type(None)):
        return jtu.tree_map(func, lhs, is_leaf=is_leaf)
    raise NotImplementedError(f"rhs of type {type(rhs)} is not implemented.")


def _append_math_op(func):
    """binary and unary magic operations"""

    @ft.wraps(func)
    def wrapper(self, rhs=None):
        return _dispatched_op_tree_map(func, self, rhs)

    return wrapper


def treeclass(cls):
    """Decorator to make a class a treeclass"""
    dcls = dc.dataclass(
        init="__init__" not in vars(cls),  # if __init__ is defined, do not overwrite it
        repr=False,  # repr is handled by _treePretty
        eq=False,  # eq is handled by _treeOp
        order=False,  # order is handled by _treeOp
        unsafe_hash=False,  # unsafe_hash is handled by _treeOp
        frozen=True,  # frozen is handled by _setter/_delattr
    )(cls)

    attrs = dict(
        __new__=_mutable(_new),  # overwrite __new__ to initialize instance variables
        __init__=_mutable(cls.__init__),  # make it mutable during initialization
        __setattr__=_setattr,  # disable direct attribute setting unless __immutable_treeclass__ is False
        __delattr__=_delattr,  # disable direct attribute deletion unless __immutable_treeclass__ is False
        __repr__=tree_repr,  # pretty print the tree representation
        __str__=tree_str,  # pretty print the tree
        __hash__=_hash,  # hash the tree
        __copy__=_copy,  # copy the tree
        __abs__=_append_math_op(op.abs),  # abs the tree leaves
        __add__=_append_math_op(op.add),  # add to the tree leaves
        __radd__=_append_math_op(op.add),  # add to the tree leaves
        __and__=_append_math_op(op.and_),  # and the tree leaves
        __rand__=_append_math_op(op.and_),  # and the tree leaves
        __eq__=_append_math_op(op.eq),  # = the tree leaves
        __floordiv__=_append_math_op(op.floordiv),  # // the tree leaves
        __ge__=_append_math_op(op.ge),  # >= the tree leaves
        __gt__=_append_math_op(op.gt),  # > the tree leaves
        __inv__=_append_math_op(op.inv),  # ~ the tree leaves
        __invert__=_append_math_op(op.invert),  # invert the tree leaves
        __le__=_append_math_op(op.le),  # <= the tree leaves
        __lshift__=_append_math_op(op.lshift),  # lshift the tree leaves
        __lt__=_append_math_op(op.lt),  # < the tree leaves
        __matmul__=_append_math_op(op.matmul),  # matmul the tree leaves
        __mod__=_append_math_op(op.mod),  # % the tree leaves
        __mul__=_append_math_op(op.mul),  # * the tree leaves
        __rmul__=_append_math_op(op.mul),  # * the tree leaves
        __ne__=_append_math_op(op.ne),  # != the tree leaves
        __neg__=_append_math_op(op.neg),  # - the tree leaves
        __not__=_append_math_op(op.not_),  # not the tree leaves
        __or__=_append_math_op(op.or_),  # or the tree leaves
        __pos__=_append_math_op(op.pos),  # + the tree leaves
        __pow__=_append_math_op(op.pow),  # ** the tree leaves
        __rshift__=_append_math_op(op.rshift),  # rshift the tree leaves
        __sub__=_append_math_op(op.sub),  # - the tree leaves
        __rsub__=_append_math_op(op.sub),  # - the tree leaves
        __truediv__=_append_math_op(op.truediv),  # / the tree leaves
        __xor__=_append_math_op(op.xor),  # xor the tree leaves
        tree_flatten=_flatten,  # jax.tree_util.tree_flatten rule
        tree_unflatten=classmethod(_unflatten),  # jax.tree_util.tree_unflatten rule
        at=property(_at_indexer),  # indexer to access a node in the tree
    )

    dcls = type(cls.__name__, (dcls,), attrs)
    return jtu.register_pytree_node_class(dcls)


def is_treeclass_equal(lhs, rhs):
    """Assert if two treeclasses are equal"""
    lhs_leaves, lhs_treedef = jtu.tree_flatten(lhs)
    rhs_leaves, rhs_treedef = jtu.tree_flatten(rhs)

    def is_node_equal(lhs_node, rhs_node):
        if isinstance(lhs_node, jnp.ndarray) and isinstance(rhs_node, jnp.ndarray):
            return jnp.array_equal(lhs_node, rhs_node)
        return lhs_node == rhs_node

    return (lhs_treedef == rhs_treedef) and all(
        [is_node_equal(lhs_leaves[i], rhs_leaves[i]) for i in range(len(lhs_leaves))]
    )
