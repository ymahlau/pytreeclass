from __future__ import annotations

import functools as ft
import operator as op
from math import ceil, floor, trunc
from typing import Any, Callable, KeysView, ValuesView

import jax.tree_util as jtu
import numpy as np

from pytreeclass._src.tree_decorator import (
    _FIELD_MAP,
    _FROZEN,
    _NOT_SET,
    _POST_INIT,
    _VARS,
    _WRAPPED,
    Field,
    _generate_field_map,
    _generate_init,
)
from pytreeclass._src.tree_freeze import _tree_hash, _tree_unwrap
from pytreeclass._src.tree_indexer import tree_indexer
from pytreeclass._src.tree_pprint import tree_repr, tree_str
from pytreeclass._src.tree_trace import register_pytree_node_trace

PyTree = Any


def _tree_unflatten(klass: type, treedef: Any, leaves: list[Any]):
    """Unflatten rule for `treeclass` to use with `jax.tree_unflatten`"""
    # call the wrapped `__new__` method (non-field initializer)
    tree = getattr(klass.__new__, _WRAPPED)(klass)
    # update through vars, to avoid calling the `setattr` method
    # that will iterate over the fields
    getattr(tree, _VARS).update(treedef[1])
    getattr(tree, _VARS).update(zip(treedef[0], leaves))
    return tree


def _tree_flatten(
    tree: PyTree,
) -> tuple[ValuesView[Any], tuple[KeysView[str], dict[str, Any]]]:
    """Flatten rule for `treeclass` to use with `jax.tree_flatten`"""
    static, dynamic = dict(getattr(tree, _VARS)), dict()
    for key in getattr(tree, _FIELD_MAP):
        dynamic[key] = static.pop(key)
    return dynamic.values(), (dynamic.keys(), static)


def _tree_trace(
    tree: PyTree,
) -> list[tuple[Any, Any, tuple[int, int], Any]]:
    """Trace flatten rule to be used with the `tree_trace` module"""
    leaves, (keys, _) = _tree_flatten(tree)
    names = (f"{key}" for key in keys)
    types = (type(leaf) for leaf in leaves)
    indices = ((i, len(leaves)) for i in range(len(leaves)))
    metadatas = ({"repr": getattr(tree, _FIELD_MAP)[key].repr} for key in keys)
    return [*zip(names, types, indices, metadatas)]


@ft.lru_cache(maxsize=None)
def _register_treeclass(klass):
    # register a treeclass only once by using `lru_cache`
    # there are two cases where a class is registered more than once:
    # first, when a class is decorated with `treeclass` more than once (e.g. `treeclass(treeclass(Class))`)
    # second when a class is decorated with `treeclass` and has a parent class that is also decorated with `treeclass`
    # in that case `__init_subclass__` registers the class before the decorator registers it.
    # this can be also be done using metaclass that registers the class on initialization
    # but we are trying to stay away from deep magic.
    jtu.register_pytree_node(klass, _tree_flatten, ft.partial(_tree_unflatten, klass))  # type: ignore
    register_pytree_node_trace(klass, _tree_trace)  # type: ignore
    return klass


def _getattr_wrapper(getattr_func):
    @ft.wraps(getattr_func)
    def getattr_method(self, key: str) -> Any:
        # this current approach replaces the older metdata based approach
        # that is used in `dataclasses`-based libraries like `flax.struct.dataclass` and v0.1 of `treeclass`.
        # the metadata approach is defined at class variable and can not be changed at runtime while the current
        # approach is more flexible because it can be changed at runtime using `tree_map` or by using `at`
        # moreover, metadata-based approach falls short when handling nested data structures values.
        # for example if a field value is a tuple of (1, 2, 3), then metadata-based approach will only be able
        # to freeze the whole tuple, but not its elements.
        # with the current approach, we can use `tree_map`/ or direct application to freeze certain tuple elements
        # and leave the rest of the tuple as is.
        # another pro of the current approach is that the field metadata is not checked during flattening/unflattening
        # so in essence, it's more efficient than the metadata-based approach during applying `jax` transformations
        # that flatten/unflatten the tree.
        # Example: when fetching `tree.a` it will be unwrapped
        # >>> @pytc.treeclass
        # ... class Tree:
        # ...    a:int = pytc.freeze(1)
        # >>> tree = Tree()
        # >>> tree
        # Tree(a=#1)  # frozen value is displayed in the repr with a prefix `#`
        # >>> tree.a
        # 1  # the value is unwrapped when accessed directly
        value = getattr_func(self, key)
        # unwrap non-`treeclass` wrapped instance variables
        # so the getattr will always return unwrapped values.
        # this renders the wrapped instance variables transparent to the user
        return _tree_unwrap(value) if key in getattr_func(self, _VARS) else value

    return getattr_method


def _setattr(tree: PyTree, key: str, value: Any) -> None:
    """Set the attribute of the tree if the tree is not frozen"""
    if getattr(tree, _FROZEN):
        msg = f"Cannot set {key}={value!r}. Use `.at['{key}'].set({value!r})` instead."
        raise AttributeError(msg)

    # apply the callbacks on setting the value
    # check if the key is a field name
    if key in getattr(tree, _FIELD_MAP):
        # check if there is a callback associated with the field
        callbacks = getattr(tree, _FIELD_MAP)[key].callbacks

        if callbacks is not None:
            for callback in callbacks:
                try:
                    # callback is a function that takes the value of the field
                    # and returns a modified value
                    value = callback(value)
                except Exception as e:
                    msg = f"Error for field=`{key}`:\n{e}"
                    raise type(e)(msg)

    # set the value
    getattr(tree, _VARS)[key] = value

    if hasattr(value, _FIELD_MAP) and (key not in getattr(tree, _FIELD_MAP)):
        field = Field(name=key, type=type(value))  # type: ignore
        # register it to field map, to avoid re-registering it in field_map
        getattr(tree, _FIELD_MAP)[key] = field


def _delattr(tree, key: str) -> None:
    """Delete the attribute of the  if tree is not frozen"""
    if getattr(tree, _FROZEN):
        raise AttributeError(f"Cannot delete {key}.")
    del getattr(tree, _VARS)[key]


def _new_wrapper(new_func: Callable) -> Callable:
    @ft.wraps(new_func)
    def new_method(klass: type, *_, **__) -> PyTree:
        tree = new_func(klass)
        for field in getattr(klass, _FIELD_MAP).values():
            if field.default is not _NOT_SET:
                getattr(tree, _VARS)[field.name] = field.default
            elif field.default_factory is not None:
                getattr(tree, _VARS)[field.name] = field.default_factory()

        # set the tree as not frozen to enable
        getattr(tree, _VARS)[_FROZEN] = False
        return tree

    # wrap the original `new_func`, to use it later in `tree_unflatten`
    # to avoid repeating iterating over fields and setting default values
    setattr(new_method, _WRAPPED, new_func)
    return new_method


def _init_sub_wrapper(init_subclass_func: Callable) -> Callable:
    @classmethod  # type: ignore
    @ft.wraps(init_subclass_func)
    def _init_subclass(klass: type) -> None:
        # Non-decorated subclasses uses the base `treeclass` leaves only
        # this behavior is aligned with `dataclasses` not registering non-decorated
        # subclasses dataclass fields. for example:
        # >>> @treeclass
        # ... class A:
        # ...   a:int=1
        # >>> class B(A):
        # ...    b:int=2
        # >>> tree = B()
        # >>> jax.tree_leaves(tree)
        # [1]
        # however if we decorate `B` with `treeclass` then the fields of `B` will be registered as leaves
        # >>> @treeclass
        # ... class B(A):
        # ...    b:int=2
        # >>> tree = B()
        # >>> jax.tree_leaves(tree)
        # [1, 2]
        # this behavior is different from `flax.struct.dataclass`
        # as it does not register non-decorated subclasses field that inherited from decorated subclasses.
        init_subclass_func()
        return _register_treeclass(klass)

    return _init_subclass


def _init_wrapper(init_func: Callable) -> Callable:
    @ft.wraps(init_func)
    def init_method(self, *a, **k) -> None:
        getattr(self, _VARS)[_FROZEN] = False
        output = init_func(self, *a, **k)

        # in case __post_init__ is defined then call it
        # after the tree is initialized
        # here, we assume that __post_init__ is a method
        if hasattr(type(self), _POST_INIT):
            # in case we found post_init in super class
            # then defreeze it first and call it
            # this behavior is differet to `dataclasses` with `frozen=True`
            # but similar if `frozen=False`
            # vars(self)[_FROZEN] = False
            # the following code will raise FrozenInstanceError in `dataclasses`
            # but it will work in `treeclass`,
            # i.e. `treeclass` defreezes the tree after `__post_init__`
            # >>> @dc.dataclass(frozen=True)
            # ... class Test:
            # ...    a:int = 1
            # ...    def __post_init__(self):
            # ...        self.b = 1
            getattr(self, _VARS)[_FROZEN] = False
            output = getattr(type(self), _POST_INIT)(self)

        # handle uninitialized fields
        for field in getattr(self, _FIELD_MAP).values():
            if field.name not in getattr(self, _VARS):
                # at this point, all fields should be initialized
                # in principle, this error will be caught when invoking `repr`/`str`
                # like in `dataclasses` but we raise it here for better error message.
                raise AttributeError(f"field=`{field.name}` is not initialized.")

        # delete the shadowing `__dict__` attribute to
        # restore the frozen behavior
        if _FROZEN in getattr(self, _VARS):
            del getattr(self, _VARS)[_FROZEN]
        return output

    return init_method


def _validate_class(klass: type) -> type:
    if not isinstance(klass, type):
        raise TypeError(f"Expected `class` but got `{type(klass)}`.")

    for key, method in zip(("__delattr__", "__setattr__"), (_delattr, _setattr)):
        if key in getattr(klass, _VARS) and getattr(klass, _VARS)[key] is not method:
            # raise error if the current setattr/delattr is not immutable
            raise AttributeError(f"Cannot define `{key}` in {klass.__name__}.")

    return klass


def _dataclass_transform(klass: type) -> type:
    # add custom `dataclass`-like fields map
    setattr(klass, _FIELD_MAP, _generate_field_map(klass))
    # flag for the immutable behavior used throughout the code
    setattr(klass, _FROZEN, True)

    if "__init__" not in getattr(klass, _VARS):
        # generate the init method in case it is not defined by the user
        setattr(klass, "__init__", _generate_init(klass))

    for name, wrapper in zip(
        ("__new__", "__init__", "__init_subclass__", "__getattribute__"),
        (_new_wrapper, _init_wrapper, _init_sub_wrapper, _getattr_wrapper),
    ):
        # wrap the original methods to enable the field initialization,
        # callback functionality and immutable behavior
        setattr(klass, name, wrapper(getattr(klass, name)))

    # immutable attributes similar to `dataclasses`
    setattr(klass, "__setattr__", _setattr)
    setattr(klass, "__delattr__", _delattr)

    # used with `match` functionality in python 3.10
    keys = tuple(key for key in getattr(klass, _FIELD_MAP))
    setattr(klass, "__match_args__", keys)

    return klass


def _tree_copy(tree: PyTree) -> PyTree:
    """Return a copy of the tree"""
    return jtu.tree_unflatten(*jtu.tree_flatten(tree)[::-1])  # type: ignore


def is_tree_equal(lhs: Any, rhs: Any) -> bool:
    """Assert if two pytrees are equal"""
    lhs_leaves, lhs_treedef = jtu.tree_flatten(lhs)
    rhs_leaves, rhs_treedef = jtu.tree_flatten(rhs)

    if not (lhs_treedef == rhs_treedef):
        # not matching treedefs
        return False

    for lhs, rhs in zip(lhs_leaves, rhs_leaves):
        if hasattr(lhs, "shape") and hasattr(lhs, "dtype"):
            if hasattr(rhs, "shape") and hasattr(rhs, "dtype"):
                if not np.array_equal(lhs, rhs):
                    # lhs array leaf is not equal to rhs array leaf
                    return False
            else:
                # lhs array leaf is an array but rhs
                # leaf is not an array
                return False
        else:
            if lhs != rhs:
                # non-array lhs leaf is not equal to the
                # non-array rhs leaf
                return False
    return True


def _unary_leafwise(func):
    def wrapper(self):
        return jtu.tree_map(func, self)

    return ft.wraps(func)(wrapper)


def _binary_leafwise(func):
    def wrapper(lhs, rhs=None):
        if isinstance(rhs, type(lhs)):
            return jtu.tree_map(func, lhs, rhs)
        return jtu.tree_map(lambda x: func(x, rhs), lhs)

    return ft.wraps(func)(wrapper)


def _swop(func):
    # swaping the arguments of a two-arg function
    return ft.wraps(func)(lambda lhs, rhs: func(rhs, lhs))


def _auxiliary_transform(klass: type, *, leafwise: bool, indexing: bool) -> type:
    # optional methods defines pretty printing, hashing,
    # copying, indexing and math operations
    # keep the original methods if they are defined by the user
    attrs = dict()

    # pretty printing
    attrs["__repr__"] = tree_repr
    attrs["__str__"] = tree_str

    # hashing and copying
    attrs["__copy__"] = _tree_copy  # type: ignore
    attrs["__hash__"] = _tree_hash  # type: ignore

    # default equality behavior if `leafwise`=False
    attrs["__eq__"] = is_tree_equal  # type: ignore

    if indexing:
        # indexing defines `at` functionality to
        # index a PyTree by integer, name, or by a boolean mask
        attrs["at"] = property(tree_indexer)  # type: ignore

    if leafwise:
        attrs["__abs__"] = _unary_leafwise(op.abs)
        attrs["__add__"] = _binary_leafwise(op.add)
        attrs["__and__"] = _binary_leafwise(op.and_)
        attrs["__ceil__"] = _unary_leafwise(ceil)
        attrs["__divmod__"] = _binary_leafwise(divmod)
        attrs["__eq__"] = _binary_leafwise(op.eq)
        attrs["__floor__"] = _unary_leafwise(floor)
        attrs["__floordiv__"] = _binary_leafwise(op.floordiv)
        attrs["__ge__"] = _binary_leafwise(op.ge)
        attrs["__gt__"] = _binary_leafwise(op.gt)
        attrs["__invert__"] = _unary_leafwise(op.invert)
        attrs["__le__"] = _binary_leafwise(op.le)
        attrs["__lshift__"] = _binary_leafwise(op.lshift)
        attrs["__lt__"] = _binary_leafwise(op.lt)
        attrs["__matmul__"] = _binary_leafwise(op.matmul)
        attrs["__mod__"] = _binary_leafwise(op.mod)
        attrs["__mul__"] = _binary_leafwise(op.mul)
        attrs["__ne__"] = _binary_leafwise(op.ne)
        attrs["__neg__"] = _unary_leafwise(op.neg)
        attrs["__or__"] = _binary_leafwise(op.or_)
        attrs["__pos__"] = _unary_leafwise(op.pos)
        attrs["__pow__"] = _binary_leafwise(op.pow)
        attrs["__radd__"] = _binary_leafwise(_swop(op.add))
        attrs["__rand__"] = _binary_leafwise(_swop(op.and_))
        attrs["__rdivmod__"] = _binary_leafwise(_swop(divmod))
        attrs["__rfloordiv__"] = _binary_leafwise(_swop(op.floordiv))
        attrs["__rlshift__"] = _binary_leafwise(_swop(op.lshift))
        attrs["__rmatmul__"] = _binary_leafwise(_swop(op.matmul))
        attrs["__rmod__"] = _binary_leafwise(_swop(op.mod))
        attrs["__rmul__"] = _binary_leafwise(_swop(op.mul))
        attrs["__ror__"] = _binary_leafwise(_swop(op.or_))
        attrs["__round__"] = _binary_leafwise(round)
        attrs["__rpow__"] = _binary_leafwise(_swop(op.pow))
        attrs["__rrshift__"] = _binary_leafwise(_swop(op.rshift))
        attrs["__rshift__"] = _binary_leafwise(op.rshift)
        attrs["__rsub__"] = _binary_leafwise(_swop(op.sub))
        attrs["__rtruediv__"] = _binary_leafwise(_swop(op.truediv))
        attrs["__rxor__"] = _binary_leafwise(_swop(op.xor))
        attrs["__sub__"] = _binary_leafwise(op.sub)
        attrs["__truediv__"] = _binary_leafwise(op.truediv)
        attrs["__trunc__"] = _unary_leafwise(trunc)
        attrs["__xor__"] = _binary_leafwise(op.xor)

    for key in attrs:
        if key not in getattr(klass, _VARS):
            # do not override any user defined methods
            # this behavior similar is to `dataclasses.dataclass`
            setattr(klass, key, attrs[key])
    return klass


def treeclass(klass: type, *, leafwise: bool = False, indexing: bool = False) -> type:
    """Decorator to convert a class to a JAX compatible tree structure.

    Args:
        klass: class to be converted to a `treeclass`
        leafwise: Wether to generate leafwise math operations methods. Defaults to `False`.
        indexing: Wether to support integer,name, or boolean indexing. Defaults to `False`.

    Example:
        >>> import functools as ft
        >>> import jax
        >>> import pytreeclass as pytc

        **Tree leaves are defined by annotated fields**
        >>> @pytc.treeclass
        ... class Tree:
        ...     a:int = 1
        ...     b:float = 2.0

        >>> tree = Tree()
        >>> jax.tree_util.tree_leaves(tree)
        [1, 2.0]

        Leaf-wise math operations are supported by setting `leafwise=True`
        >>> @ft.partial(pytc.treeclass, leafwise=True)
        ... class Tree:
        ...     a:int = 1
        ...     b:float = 2.0

        >>> tree = Tree()
        >>> tree + 1
        Tree(a=2, b=3.0)


        Advanced indexing is supported by setting `indexing=True`
        >>> @ft.partial(pytc.treeclass, indexing=True)
        ... class Tree:
        ...     a:int = 1
        ...     b:float = 2.0

        >>> tree = Tree()
        >>> tree.at[0].get()
        Tree(a=1, b=None)

        >>> tree.at["a"].get()
        Tree(a=1, b=None)

        Indexing is supported for {`list`, `tuple`, `dict`, `defaultdict`, `OrderedDict`, 'namedtuple'}
        and `treeclass` wrapped classes.

        Extending indexing to other types is possible by registering the type with
        `pytreeclass.register_pytree_node_trace`

    Raises:
        TypeError: if the input class is not a `class`
    """
    # check if the input is a valid class
    # in essence, it should be a type with immutable setters and deleters
    klass = _validate_class(klass)

    # add the immutable setters and deleters
    # and generate the `__init__` method if not present
    # generate fields from type annotations
    klass = _dataclass_transform(klass)

    # add the optional methods to the class
    # optional methods are math operations, indexing and masking,
    # hashing and copying, and pretty printing
    klass = _auxiliary_transform(klass, leafwise=leafwise, indexing=indexing)

    # add the class to the `JAX` registry if not registered
    return _register_treeclass(klass)
