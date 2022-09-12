from __future__ import annotations

import functools as ft
from collections.abc import Iterable
from dataclasses import Field, field
from types import FunctionType
from typing import Any, Callable

import jax.numpy as jnp
import jax.tree_util as jtu

from pytreeclass._src.dispatch import dispatch
from pytreeclass._src.tree_util import (
    _pytree_map,
    _tree_immutate,
    _tree_mutate,
    is_treeclass,
)

PyTree = Any


def static_field(**kwargs):
    """ignore from pytree computations"""
    return field(**{**kwargs, **{"metadata": {"static": True}}})


def _mutable(func):
    """decorator that allow mutable behvior
    for class methods/ function with treeclass as first argument

    Example:
        class ... :

        >>> @_mutable
        ... def mutable_method(self):
        ...    return self.value + 1
    """
    assert isinstance(
        func, FunctionType
    ), f"mutable can only be applied to methods. Found{type(func)}"

    @ft.wraps(func)
    def mutable_method(self, *args, **kwargs):
        self = _tree_mutate(tree=self)
        output = func(self, *args, **kwargs)
        self = _tree_immutate(tree=self)
        return output

    return mutable_method


class cached_method:
    def __init__(self, func):
        self.name = func.__name__
        self.func = func

    def __get__(self, instance, owner):
        output = self.func(instance)
        cached_func = ft.wraps(self.func)(lambda *args, **kwargs: output)
        object.__setattr__(instance, self.name, cached_func)
        return cached_func


def _is_nondiff(item):
    """check if tree is non-differentiable"""

    def _is_nondiff_item(node):
        """check if node is non-differentiable"""
        # non-differentiable types
        if isinstance(node, (int, bool, str)):
            return True

        # non-differentiable array
        elif isinstance(node, jnp.ndarray) and not jnp.issubdtype(
            node.dtype, jnp.inexact
        ):
            return True

        # non-differentiable type
        elif isinstance(node, Callable) and not is_treeclass(node):
            return True

        return False

    if isinstance(item, Iterable):
        # if an iterable has at least one non-differentiable item
        # then the whole iterable is non-differentiable
        return jtu.tree_all(jtu.tree_map(_is_nondiff_item, item))
    return _is_nondiff_item(item)


def filter_nondiff(tree, where: PyTree | None = None):
    # we use _pytree_map to add {nondiff:True} to a non-differentiable field metadata
    # this operation is done in-place and changes the tree structure
    # thus its not bundled with `.at[..]` as it will break composability

    @dispatch(argnum=1)
    def _static(tree, where: PyTree | None):
        raise TypeError(
            f"where must be a PyTree of same structure. Found {type(where)}"
        )

    @_static.register(type(None))
    def _(tree, where: None):
        """filter non-differentiable fields from a treeclass instance

        Note:
            Mark fields as non-differentiable with adding metadata `dict(static=True,nondiff=True)`
            the way it is done is by adding a field `__undeclared_fields__` to the treeclass instance
            that contains the non-differentiable fields.
            This is done to avoid mutating the original treeclass class or copying .

            during the `tree_flatten`, tree_fields are the combination of
            {__dataclass_fields__, __undeclared_fields__} this means that a field defined
            in `__undeclared_fields__` with the same name as in __dataclass_fields__
            will override its properties, this is useful if you want to change the metadata
            of a field but don't want to change the original field definition.

        Example:
            @pytc.treeclass
            class Linear:
                weight: jnp.ndarray
                bias: jnp.ndarray
                other: jnp.ndarray = (1,2,3,4)
                a: int = 1
                b: float = 1.0
                c: int = 1
                d: float = 2.0
                act : Callable = jax.nn.tanh

                def __init__(self,in_dim,out_dim):
                    self.weight = jnp.ones((in_dim,out_dim))
                    self.bias =  jnp.ones((1,out_dim))

                def __call__(self,x):
                    return self.act(self.b+x)

            @jax.value_and_grad
            def loss_func(model):
                return jnp.mean((model(1.)-0.5)**2)

            @jax.jit
            def update(model):
                value,grad = loss_func(model)
                return value,model-1e-3*grad

            def train(model,epochs=10_000):
                model = filter_nondiff(model)
                for _ in range(epochs):
                    value,model = update(model)
                return model

            >>> model = Linear(1,1)
            >>> model = train(model)
            >>> print(model)
            # Linear(
            #   weight=[[1.]],
            #   bias=[[1.]],
            #   *other=(1,2,3,4),
            #   *a=1,
            #   b=-0.36423424,
            #   *c=1,
            #   d=2.0,
            #   *act=tanh(x)
            # )
        """
        # this dispatch filter the non-differentiable fields from the tree based
        # on the node value.
        # in essence any field with non-differentiable value is filtered out.
        return _pytree_map(
            tree,
            # condition is a lambda function that returns True if the field is non-differentiable
            cond=lambda _, __, node_item: _is_nondiff(node_item),
            # Extends the field metadata to add {nondiff:True}
            true_func=lambda tree, field_item, node_item: {
                **tree.__undeclared_fields__,
                **{
                    field_item.name: _copy_field(
                        field_item, field_aux_metadata={"static": True, "nondiff": True}
                    )
                },
            },
            # keep the field as is
            false_func=lambda tree, field_item, node_item: tree.__undeclared_fields__,
            attr_func=lambda _, __, ___: "__undeclared_fields__",
            # do not recurse if the field is `static`
            is_leaf=lambda _, field_item, __: field_item.metadata.get("static", False),
        )

    @_static.register(type(tree))
    def _(tree, where: PyTree):
        """
        this dispatch marks fields as non-differentiable by using `where` mask.

        Example:
            @pytc.treeclass
            class L0:
                a: int = 1
                b: int = 2
                c: int = 3

            @pytc.treeclass
            class L1:
                a: int = 1
                b: int = 2
                c: int = 3
                d: L0 = L0()

            @pytc.treeclass
            class L2:
                a: int = 10
                b: int = 20
                c: int = 30
                d: L1 = L1()

            >>> t = L2()

            >>> print(t.tree_diagram())
            # L2
            #     ├── a=10
            #     ├── b=20
            #     ├── c=30
            #     └── d=L1
            #         ├── a=1
            #         ├── b=2
            #         ├── c=3
            #         └── d=L0
            #             ├── a=1
            #             ├── b=2
            #             └── c=3

            # Let's mark `a` and `b`  in `L0` as non-differentiable
            # we can do this by using `where` mask

            >>> t = t.at["d"].at["d"].set(filter_nondiff(t.d.d, where= L0(a=True,b=True, c=False)))

            >>> print(t.tree_diagram())
            # L2
            #     ├── a=10
            #     ├── b=20
            #     ├── c=30
            #     └── d=L1
            #         ├── a=1
            #         ├── b=2
            #         ├── c=3
            #         └── d=L0
            #             ├*─ a=1
            #             ├*─ b=2
            #             └── c=3
            # note `*` indicates that the field is non-differentiable
        """
        # this dispaatch sets field non-differentiable if the corresponding field in where is True
        # in essence `where` is a mask of the same structure as the tree that indicates which fields
        # to filter out from gradient computation.
        return _pytree_map(
            tree,
            # condition is a PyTree of booleans that indicates which fields to filter out
            cond=where,
            # Extends the field metadata to add {nondiff:True}
            true_func=lambda tree, field_item, node_item: {
                **tree.__undeclared_fields__,
                **{
                    field_item.name: _copy_field(
                        field_item, field_aux_metadata={"static": True, "nondiff": True}
                    )
                },
            },
            # keep the field as is
            false_func=lambda tree, field_item, node_item: tree.__undeclared_fields__,
            # the attribute to update is `__undeclared_fields__`
            attr_func=lambda _, __, ___: "__undeclared_fields__",
            # do not recurse if the field is `static`
            is_leaf=lambda _, field_item, __: field_item.metadata.get("static", False),
        )

    return _static(tree, where)


def unfilter_nondiff(tree):
    """remove fields added by `filter_nondiff"""
    return _pytree_map(
        tree,
        cond=lambda _, __, ___: True,
        true_func=lambda tree, field_item, node_item: {
            field_name: field_value
            for field_name, field_value in tree.__undeclared_fields__.items()
            if not field_value.metadata.get("nondiff", False)
        },
        false_func=lambda _, __, ___: {},
        attr_func=lambda _, __, ___: "__undeclared_fields__",
        is_leaf=lambda _, __, ___: False,
    )


def _copy_field(
    field_item,
    *,
    field_name: str = None,
    field_type: type = None,
    field_compare: bool = None,
    field_default: Any = None,
    field_default_factory: Callable = None,
    field_hash: Callable = None,
    field_init: bool = None,
    field_repr: bool = None,
    field_metadata: dict[str, Any] = None,
    field_aux_metadata: dict[str, Any] = None,
):
    assert isinstance(
        field_item, Field
    ), f"field_item must be a dataclass field. Found {field_item}"
    """copy a field with new values"""
    # creation of a new field avoid mutating the original field
    field_aux_metadata = field_aux_metadata or {}
    new_field = field(
        compare=field_compare or getattr(field_item, "compare"),
        default=field_default or getattr(field_item, "default"),
        default_factory=field_default_factory or getattr(field_item, "default_factory"),
        hash=field_hash or getattr(field_item, "hash"),
        init=field_init or getattr(field_item, "init"),
        metadata=field_metadata
        or {
            **getattr(field_item, "metadata"),
            **field_aux_metadata,
        },
        repr=field_repr or getattr(field_item, "repr"),
    )

    object.__setattr__(new_field, "name", field_name or getattr(field_item, "name"))
    object.__setattr__(new_field, "type", field_type or getattr(field_item, "type"))

    return new_field
