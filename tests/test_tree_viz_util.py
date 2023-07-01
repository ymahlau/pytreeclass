# Copyright 2023 PyTreeClass authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import jax
import jax.nn.initializers as ji
import jax.tree_util as jtu

from pytreeclass._src.tree_pprint import _hbox, _hstack, _table, _vbox, func_pp, pp


def test_vbox():
    assert _vbox("a", " a", "a ") == "┌──┐\n│a │\n├──┤\n│ a│\n├──┤\n│a │\n└──┘"
    assert _vbox("a", "b") == "┌─┐\n│a│\n├─┤\n│b│\n└─┘"


def test_hbox():
    assert _hbox("a", "b", "c") == "┌─┬─┬─┐\n│a│b│c│\n└─┴─┴─┘"
    assert _hbox("a") == "┌─┐\n│a│\n└─┘"


def test_table():
    col1 = ["1\n", "2"]
    col2 = ["3", "4000"]
    assert (
        _table([col1, col2], transpose=True)
        == "┌─┬────┐\n│1│3   │\n│ │    │\n├─┼────┤\n│2│4000│\n└─┴────┘"
    )


def test_hstack():
    assert _hstack(_hbox("a"), _vbox("b", "c")) == "┌─┬─┐\n│a│b│\n└─┼─┤\n  │c│\n  └─┘"


def test_func_pp():
    def example(a: int, b=1, *c, d, e=2, **f) -> str:
        ...  # fmt: skip

    assert (
        func_pp(example, indent=0, kind="str", width=60, depth=0)
        == "example(a, b, *c, d, e, **f)"
    )
    # assert (
    #     func_pp(lambda x: x, indent=0, kind="str", width=60, depth=0)
    #     == "Lambda(x)"
    # )
    assert (
        func_pp(jax.nn.relu, indent=0, kind="str", width=60, depth=0)
        == "relu(*args, **kwargs)"
    )
    assert (
        pp(jtu.Partial(jax.nn.relu), indent=0, kind="str", width=60, depth=0)
        == "Partial(relu(*args, **kwargs))"
    )
    assert (
        pp(jtu.Partial(jax.nn.relu), indent=0, kind="str", width=60, depth=0)
        == "Partial(relu(*args, **kwargs))"
    )
    assert (
        func_pp(ji.he_normal, indent=0, kind="str", width=60, depth=0)
        == "he_normal(in_axis, out_axis, batch_axis, dtype)"
    )

    # assert (
    #     _pprint(jax.jit(lambda x: x), indent=0, kind="repr", width=60, depth=0)
    #     == "jit(Lambda(x))"
    # )
