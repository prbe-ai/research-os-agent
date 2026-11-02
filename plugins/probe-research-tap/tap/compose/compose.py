"""compose(): fold a class's part classes into its own namespace, so a class split across files
is, at runtime, the class it was before the split."""

from __future__ import annotations

import functools
import inspect
from typing import Any, TypeVar

_Class = TypeVar("_Class", bound=type)

#: Entries Python makes for each class itself; a part's are never copied.
_PER_CLASS = frozenset(
    {
        "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__",
        "__static_attributes__", "__annotations__", "__annotate__", "__annotate_func__",
        "__annotations_cache__", "__orig_bases__", "__type_params__", "__classdictcell__",
    }
)


def compose(cls: _Class) -> _Class:
    """`cls` rebuilt with every base's members in its own namespace and `object` as its only base.

    The bases are its parts: plain classes whose methods run with `self` the composed class.
    Editors and type checkers see them as bases; at runtime `__bases__`, `__mro__` and
    `vars(cls)` are what the unsplit class had, so patches, `__dict__` reads and the public API
    snapshot do not change. Refused: a part with bases of its own, a name defined twice, and
    zero-argument `super()` (its `__class__` would be the part, not the composed class).
    """
    namespace: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    for part in cls.__bases__:
        if part.__bases__ != (object,):
            raise TypeError(f"{cls.__name__}: part {part.__name__} has bases of its own")
        annotations.update(inspect.get_annotations(part))
        for name, value in vars(part).items():
            if name in _PER_CLASS:
                continue
            if name in namespace:
                raise TypeError(f"{cls.__name__}: two parts define {name!r}")
            namespace[name] = value
    annotations.update(inspect.get_annotations(cls))
    for name, value in vars(cls).items():
        if name in ("__dict__", "__weakref__", "__annotate__", "__annotate_func__", "__annotations_cache__"):
            continue
        if name in namespace:
            raise TypeError(f"{cls.__name__}: {name!r} is defined in the class and in a part")
        namespace[name] = value
    for name, value in namespace.items():
        if _uses_class_cell(value):
            raise TypeError(f"{cls.__name__}.{name}: zero-argument super() or __class__ in a composed class")
    if annotations:
        namespace["__annotations__"] = annotations
    return type(cls)(cls.__name__, (object,), namespace)


def fold_parts(cls: _Class, *parts: type) -> _Class:
    """compose(), in place: copy each part's members into ``cls`` itself and return it.

    For a class its parts read back by name (their methods call ``Journal.read_status``): the
    class must exist before they load, so it is defined bare and its module folds the parts in
    from its bottom imports. Same result and refusals as compose(); the class object is kept,
    so every module that imported it early holds the finished class.
    """
    own = set(vars(cls))
    annotations: dict[str, Any] = {}
    for part in parts:
        if part.__bases__ != (object,):
            raise TypeError(f"{cls.__name__}: part {part.__name__} has bases of its own")
        annotations.update(inspect.get_annotations(part))
        for name, value in vars(part).items():
            if name in _PER_CLASS:
                continue
            if name in own:
                raise TypeError(f"{cls.__name__}: {name!r} is defined twice (in the class or two parts)")
            if _uses_class_cell(value):
                raise TypeError(f"{cls.__name__}.{name}: zero-argument super() or __class__ in a composed class")
            own.add(name)
            setattr(cls, name, value)
    if annotations:
        cls.__annotations__ = {**annotations, **inspect.get_annotations(cls)}
    return cls


def _uses_class_cell(value: Any) -> bool:
    """A function (or the one inside a property, staticmethod, classmethod or wrapper) closing over `__class__`."""
    if isinstance(value, property):
        return any(_uses_class_cell(f) for f in (value.fget, value.fset, value.fdel) if f is not None)
    if isinstance(value, (staticmethod, classmethod, functools.cached_property)):
        return _uses_class_cell(getattr(value, "__func__", None) or getattr(value, "func", None))
    seen = 0
    while callable(value) and seen < 8:
        code = getattr(value, "__code__", None)
        if code is not None and "__class__" in code.co_freevars:
            return True
        value, seen = getattr(value, "__wrapped__", None), seen + 1
    return False
