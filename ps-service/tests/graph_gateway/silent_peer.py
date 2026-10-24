"""A local TCP peer that accepts connections and then never answers.

It stands in for a dependency that completes the TCP handshake but hangs: no mock is involved, the
client under test talks to a real socket. With `completes_driver_handshake` the peer answers the
FalkorDB driver's connection-setup commands and then goes silent, so a connection is established
and every later command stalls.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

_LOOPBACK = "127.0.0.1"
_READ_BYTES = 65536
_ACCEPT_POLL_SECONDS = 0.1
_CLIENT_SETINFO_REPLY = b"+OK\r\n"
_EMPTY_INFO_REPLY = b"$0\r\n\r\n"


class _SilentPeer:
    """Accepts connections on a daemon thread and holds them open without ever replying."""

    def __init__(self, *, completes_driver_handshake: bool) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind((_LOOPBACK, 0))
        self._listener.listen()
        self._listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._completes_driver_handshake = completes_driver_handshake
        self._connections: list[socket.socket] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._accept_until_stopped, daemon=True)
        self.accepted_connections = 0

    @property
    def port(self) -> int:
        """The loopback port the peer listens on."""
        return int(self._listener.getsockname()[1])

    def start(self) -> None:
        """Begin accepting connections."""
        self._thread.start()

    def stop(self) -> None:
        """Stop accepting and close every connection."""
        self._stopped.set()
        self._thread.join(timeout=5)
        for connection in self._connections:
            connection.close()
        self._listener.close()

    def _accept_until_stopped(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            self.accepted_connections += 1
            self._connections.append(connection)
            threading.Thread(target=self._answer_then_stay_silent, args=(connection,)).start()

    def _answer_then_stay_silent(self, connection: socket.socket) -> None:
        connection.settimeout(_ACCEPT_POLL_SECONDS)
        while not self._stopped.is_set():
            try:
                received = connection.recv(_READ_BYTES)
            except TimeoutError:
                continue
            except OSError:
                return
            if not received:
                return
            if self._completes_driver_handshake:
                connection.sendall(_handshake_reply(received))


def _handshake_reply(received: bytes) -> bytes:
    """Answer the setup commands found in `received`; any other command gets no reply."""
    upper = received.upper()
    return _CLIENT_SETINFO_REPLY * upper.count(b"SETINFO") + _EMPTY_INFO_REPLY * upper.count(
        b"\r\nINFO\r\n"
    )


@contextmanager
def silent_peer(*, completes_driver_handshake: bool = False) -> Generator[_SilentPeer]:
    """Run a peer on a free loopback port; yields it so a test can read `port`.

    Args:
        completes_driver_handshake: Whether the peer answers the driver's connection-setup
            commands before going silent; by default it never writes.

    Yields:
        The running peer; it is closed on exit.
    """
    peer = _SilentPeer(completes_driver_handshake=completes_driver_handshake)
    peer.start()
    try:
        yield peer
    finally:
        peer.stop()
