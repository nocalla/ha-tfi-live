"""Pytest configuration for ha_tfi_live tests.

Blocks the ``pytest-homeassistant-custom-component`` plugin at startup.  That
plugin unconditionally imports ``homeassistant.runner`` which pulls in the
POSIX-only ``fcntl`` module and crashes on Windows.  Tests use only
``unittest.mock`` and plain ``homeassistant`` package imports.

Windows socketpair shim
-----------------------
pytest-homeassistant-custom-component unconditionally calls
``pytest_socket.disable_socket()`` in its ``pytest_runtest_setup`` hook.
On Windows the ``socket.socketpair()`` fallback (used by asyncio to build
the self-pipe in both ``SelectorEventLoop`` and ``ProactorEventLoop``)
calls ``socket.socket()`` directly.  When the socket class has been replaced
by ``GuardedSocket`` this raises ``SocketBlockedError`` before any test body
even runs.

The fix: replace ``socket.socketpair`` with a wrapper that temporarily
restores the real ``socket.socket`` class for the duration of the call, then
puts the current class (real or guarded) back.
"""

import socket as _socket
import sys

_REAL_SOCKET_CLS = _socket.socket
_REAL_SOCKETPAIR = getattr(_socket, "socketpair", None)


def _socketpair_via_real_socket(
    family: int = _socket.AF_INET,
    type: int = _socket.SOCK_STREAM,
    proto: int = 0,
) -> "tuple[_socket.socket, _socket.socket]":
    """Call the OS-level socketpair using the real socket class.

    Temporarily restores ``socket.socket`` to the genuine implementation so
    that asyncio's self-pipe creation succeeds even while pytest-socket has
    installed its ``GuardedSocket`` replacement.

    Args:
        family: Address family (default AF_INET).
        type: Socket type (default SOCK_STREAM).
        proto: Protocol number (default 0).

    Returns:
        A pair of connected socket objects.
    """
    guarded = _socket.socket
    _socket.socket = _REAL_SOCKET_CLS
    try:
        if _REAL_SOCKETPAIR is not None:
            return _REAL_SOCKETPAIR(family, type, proto)
        lsock = _REAL_SOCKET_CLS(family, type, proto)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen(1)
            addr = lsock.getsockname()
            csock = _REAL_SOCKET_CLS(family, type, proto)
            try:
                csock.setblocking(False)
                try:
                    csock.connect(addr)
                except (BlockingIOError, InterruptedError):
                    pass
                csock.setblocking(True)
                ssock, _ = lsock.accept()
            except Exception:
                csock.close()
                raise
        finally:
            lsock.close()
        return ssock, csock
    finally:
        _socket.socket = guarded


if sys.platform == "win32":
    _socket.socketpair = _socketpair_via_real_socket  # type: ignore[assignment]


def pytest_configure(config):
    """Deregister the homeassistant pytest plugin before it is loaded.

    Args:
        config: The pytest Config object.
    """
    config.pluginmanager.set_blocked("homeassistant")
