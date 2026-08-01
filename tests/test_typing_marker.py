"""The distribution has to declare that it is typed, or consumers cannot see the types."""

from __future__ import annotations

import zipfile
from pathlib import Path

import memento


def test_the_package_ships_a_py_typed_marker():
    """PEP 561: without this file every annotation in the engine is invisible downstream.

    The modules are fully annotated, so the types exist — they just do not cross the package
    boundary. A consumer on `mypy --strict` gets `import-untyped` on every `from memento import
    ...`, and the usual fix is `ignore_missing_imports`, which silently turns the whole engine
    into `Any` on the consumer's side: `apply_consolidation`'s required `expected_fingerprint`,
    the `Proposal` shape, the `FieldSpec` kwargs — all unchecked, in the one place a consumer is
    most likely to get the contract wrong. jubs hit exactly this in B-01.
    """
    assert (Path(memento.__file__).parent / "py.typed").is_file()


def test_the_marker_survives_into_a_built_wheel(tmp_path):
    """Present in the source tree is not the same as shipped.

    An editable install resolves `memento.__file__` straight back to `src/`, so the test above
    would pass on a wheel that omits the marker entirely — which is the only case that matters,
    since that is what a consumer installs. This builds one and looks inside it.
    """
    import build.__main__  # noqa: F401 — presence check, the API call is below
    from build import ProjectBuilder

    root = Path(__file__).resolve().parents[1]
    wheel = Path(ProjectBuilder(root).build("wheel", str(tmp_path)))
    with zipfile.ZipFile(wheel) as zf:
        assert "memento/py.typed" in zf.namelist(), sorted(zf.namelist())
