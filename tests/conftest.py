"""Pytest configuration for tfi_live tests.

Blocks the ``pytest-homeassistant-custom-component`` plugin at startup.  That
plugin unconditionally imports ``homeassistant.runner`` which pulls in the
POSIX-only ``fcntl`` module and crashes on Windows.  The coordinator tests use
only ``unittest.mock`` and plain ``homeassistant`` package imports, so the
plugin is not needed.
"""


def pytest_configure(config):
    """Deregister the homeassistant pytest plugin before it is loaded.

    Args:
        config: The pytest Config object.
    """
    config.pluginmanager.set_blocked("homeassistant")
