"""`FalkorDBKeyPort` against a hand-written Redis/FalkorDB stand-in (issue #214 S6).

The stand-in keeps a version number per key. A pipeline that watched a key refuses to execute when
that key's version moved since the watch, as Redis does. The real engine's behaviour is proved by
`test_swap_port_live.py` (`falkordb_live`); here the port's own logic is: an absent source copies
as empty, a discard skips missing keys, the watch begins before the copies, a refused watch
becomes `SwapConflictError`, and the watch is always released.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions

from ps_service.graph_gateway.default_gateway import FalkorDBKeyPort
from ps_service.graph_gateway.errors import SwapConflictError

if TYPE_CHECKING:
    from falkordb import FalkorDB

_EMPTY_KEY_MESSAGE = "Invalid graph operation on empty key"


class _Keys:
    """The key space: key name -> version (bumped by every write)."""

    def __init__(self, *present: str) -> None:
        self.versions = dict.fromkeys(present, 1)
        self.open_pipelines = 0
        self.log: list[str] = []

    def write(self, name: str) -> None:
        self.versions[name] = self.versions.get(name, 0) + 1


class _Pipe:
    def __init__(self, keys: _Keys) -> None:
        self._keys = keys
        self._watched: dict[str, int] = {}
        self._queued: list[tuple[str, str]] = []
        keys.open_pipelines += 1

    def watch(self, *names: str) -> None:
        self._keys.log.append(f"watch {','.join(names)}")
        self._watched = {name: self._keys.versions.get(name, 0) for name in names}

    def multi(self) -> None:
        self._keys.log.append("multi")

    def rename(self, src: str, dst: str) -> object:
        self._queued.append((src, dst))
        return self

    def execute(self) -> list[object]:
        keys = self._keys
        if any(keys.versions.get(name, 0) != seen for name, seen in self._watched.items()):
            raise redis.exceptions.WatchError
        for src, dst in self._queued:
            keys.versions[dst] = keys.versions.pop(src) + 1
        keys.log.append("exec " + ",".join(f"{s}>{d}" for s, d in self._queued))
        return [True] * len(self._queued)

    def reset(self) -> None:
        self._keys.open_pipelines -= 1


class _Connection:
    def __init__(self, keys: _Keys) -> None:
        self._keys = keys

    def pipeline(self, *, transaction: bool = True) -> _Pipe:
        return _Pipe(self._keys)

    def delete(self, *names: str) -> int:
        gone = [name for name in names if name in self._keys.versions]
        for name in gone:
            del self._keys.versions[name]
        self._keys.log.append("delete " + ",".join(names))
        return len(gone)


class _Graph:
    def __init__(self, keys: _Keys, name: str) -> None:
        self._keys = keys
        self._name = name

    def copy(self, clone: str) -> object:
        if self._name not in self._keys.versions:
            raise redis.exceptions.ResponseError(_EMPTY_KEY_MESSAGE)
        self._keys.versions[clone] = 1
        self._keys.log.append(f"copy {self._name}>{clone}")
        return None

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        self._keys.write(self._name)
        self._keys.log.append(f"vivify {self._name}")
        return None


class _Db:
    def __init__(self, keys: _Keys) -> None:
        self.connection = _Connection(keys)
        self._keys = keys

    def select_graph(self, name: str) -> _Graph:
        return _Graph(self._keys, name)


def _port(keys: _Keys) -> FalkorDBKeyPort:
    return FalkorDBKeyPort(lambda: cast("FalkorDB", _Db(keys)))


def test_copy_clones_an_existing_graph() -> None:
    keys = _Keys("live")

    _port(keys).copy("live", "staged")

    assert "staged" in keys.versions
    assert "live" in keys.versions


def test_an_absent_source_copies_as_an_empty_graph() -> None:
    keys = _Keys()

    _port(keys).copy("never_touched", "staged")

    assert "staged" in keys.versions
    assert "never_touched" not in keys.versions


def test_another_engine_error_on_copy_is_not_swallowed() -> None:
    class Broken(_Graph):
        def copy(self, clone: str) -> object:
            raise redis.exceptions.ResponseError("OOM command not allowed")

    class BrokenDb(_Db):
        def select_graph(self, name: str) -> _Graph:
            return Broken(self._keys, name)

    port = FalkorDBKeyPort(lambda: cast("FalkorDB", BrokenDb(_Keys("live"))))

    with pytest.raises(redis.exceptions.ResponseError, match="OOM"):
        port.copy("live", "staged")


def test_discard_drops_what_exists_and_skips_what_does_not() -> None:
    keys = _Keys("a", "b")

    _port(keys).discard("a", "missing", "b")

    assert keys.versions == {}


def test_discard_of_nothing_sends_nothing() -> None:
    keys = _Keys("a")

    _port(keys).discard()

    assert keys.log == []


def test_the_watch_begins_before_the_copies_and_the_swap_renames_atomically() -> None:
    keys = _Keys("live_a", "live_b")
    port = _port(keys)

    with port.watching(["live_a", "live_b"]) as swapper:
        port.copy("live_a", "st_a")
        port.copy("live_b", "st_b")
        swapper.swap_in([("st_a", "live_a"), ("st_b", "live_b")])

    assert keys.log[0] == "watch live_a,live_b"
    assert keys.log[-1] == "exec st_a>live_a,st_b>live_b"
    assert set(keys.versions) == {"live_a", "live_b"}
    assert keys.open_pipelines == 0


def test_a_write_to_a_watched_key_between_the_copy_and_the_swap_is_a_conflict() -> None:
    keys = _Keys("live")
    port = _port(keys)

    with port.watching(["live"]) as swapper:
        port.copy("live", "staged")
        keys.write("live")
        with pytest.raises(SwapConflictError):
            swapper.swap_in([("staged", "live")])

    assert keys.versions["live"] == 2
    assert "staged" in keys.versions
    assert keys.open_pipelines == 0


def test_the_watch_is_released_when_the_block_raises() -> None:
    keys = _Keys("live")
    port = _port(keys)

    with pytest.raises(RuntimeError, match="boom"), port.watching(["live"]):
        raise RuntimeError("boom")

    assert keys.open_pipelines == 0


def test_a_watched_key_that_did_not_exist_and_was_created_meanwhile_is_a_conflict() -> None:
    keys = _Keys()
    port = _port(keys)

    with port.watching(["live"]) as swapper:
        port.copy("live", "staged")
        keys.write("live")
        with pytest.raises(SwapConflictError):
            swapper.swap_in([("staged", "live")])
