import jax
from jax import numpy as jnp

import pytreeclass as pytc


def test_ops_with_jit():
    class T0(pytc.TreeClass, leafwise=True):
        a: jax.Array = jnp.array(1)
        b: jax.Array = jnp.array(2)
        c: jax.Array = jnp.array(3)

    class T1(pytc.TreeClass, leafwise=True):
        a: jax.Array = jnp.array(1)
        b: jax.Array = jnp.array(2)
        c: jax.Array = jnp.array(3)
        d: jax.Array = jnp.array([1, 2, 3])

    @jax.jit
    def getter(tree):
        return tree.at[...].get()

    @jax.jit
    def setter(tree):
        return tree.at[...].set(0)

    @jax.jit
    def applier(tree):
        return tree.at[...].apply(lambda _: 0)

    # with pytest.raises(jax.errors.ConcretizationTypeError):
    pytc.is_tree_equal(getter(T0()), T0())

    assert pytc.is_tree_equal(T0(0, 0, 0), setter(T0()))

    assert pytc.is_tree_equal(T0(0, 0, 0), applier(T0()))

    # with pytest.raises(jax.errors.ConcretizationTypeError):
    pytc.is_tree_equal(getter(T1()), T1())

    assert pytc.is_tree_equal(T1(0, 0, 0, 0), setter(T1()))

    assert pytc.is_tree_equal(T1(0, 0, 0, 0), applier(T1()))

    assert jax.jit(pytc.is_tree_equal)(T1(0, 0, 0, 0), applier(T1()))
