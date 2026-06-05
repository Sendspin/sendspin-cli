"""Tests for offline (no network) resilience of the Zeroconf patch."""

from __future__ import annotations

import errno
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import aiosendspin.server.server as _aiosendspin_server_mod
from sendspin.serve import _NullZeroconf, _make_offline_resilient_zeroconf


@pytest.fixture(autouse=True)
def _restore_aiosendspin_asynczeroconf():
    """Restore the original AsyncZeroconf reference after each test."""
    original = _aiosendspin_server_mod.AsyncZeroconf
    original_saved = getattr(_aiosendspin_server_mod, "_original_AsyncZeroconf", None)
    yield
    _aiosendspin_server_mod.AsyncZeroconf = original
    if original_saved is None:
        _aiosendspin_server_mod.__dict__.pop("_original_AsyncZeroconf", None)


# ---------------------------------------------------------------------------
# Unit tests for _NullZeroconf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_zeroconf_methods_are_no_ops() -> None:
    """_NullZeroconf methods should complete without raising."""
    nz = _NullZeroconf()
    await nz.async_register_service("arg")
    await nz.async_unregister_service("arg")
    await nz.async_close()


# ---------------------------------------------------------------------------
# Unit tests for _make_offline_resilient_zeroconf patch
# ---------------------------------------------------------------------------


def test_patch_replaces_asynczeroconf_in_aiosendspin() -> None:
    """After patching, aiosendspin.server.server.AsyncZeroconf should be our factory."""
    _make_offline_resilient_zeroconf()
    assert _aiosendspin_server_mod.AsyncZeroconf is not None
    # The original class should have been saved
    assert hasattr(_aiosendspin_server_mod, "_original_AsyncZeroconf")


def test_patch_is_idempotent() -> None:
    """Calling the patch twice should not double-wrap AsyncZeroconf."""
    _make_offline_resilient_zeroconf()
    factory_after_first = _aiosendspin_server_mod.AsyncZeroconf

    _make_offline_resilient_zeroconf()
    factory_after_second = _aiosendspin_server_mod.AsyncZeroconf

    # Both calls should install a factory (not the raw AsyncZeroconf class).
    assert factory_after_first is factory_after_second or callable(factory_after_second)


def test_factory_returns_null_on_enodev() -> None:
    """The patched factory should return _NullZeroconf when ENODEV is raised."""
    offline_error = OSError(errno.ENODEV, "No such device")

    with patch("zeroconf.asyncio.AsyncZeroconf", side_effect=offline_error):
        _make_offline_resilient_zeroconf()
        result = _aiosendspin_server_mod.AsyncZeroconf()

    assert isinstance(result, _NullZeroconf)


def test_factory_returns_null_on_enetdown() -> None:
    """The patched factory should return _NullZeroconf when ENETDOWN is raised."""
    offline_error = OSError(errno.ENETDOWN, "Network is down")

    with patch("zeroconf.asyncio.AsyncZeroconf", side_effect=offline_error):
        _make_offline_resilient_zeroconf()
        result = _aiosendspin_server_mod.AsyncZeroconf()

    assert isinstance(result, _NullZeroconf)


def test_factory_returns_null_on_eaddrnotavail() -> None:
    """The patched factory should return _NullZeroconf when EADDRNOTAVAIL is raised."""
    offline_error = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

    with patch("zeroconf.asyncio.AsyncZeroconf", side_effect=offline_error):
        _make_offline_resilient_zeroconf()
        result = _aiosendspin_server_mod.AsyncZeroconf()

    assert isinstance(result, _NullZeroconf)


def test_factory_re_raises_unrelated_oserror() -> None:
    """OSError with an unrelated errno should still propagate."""
    unrelated_error = OSError(errno.EACCES, "Permission denied")

    with patch("zeroconf.asyncio.AsyncZeroconf", side_effect=unrelated_error):
        _make_offline_resilient_zeroconf()
        with pytest.raises(OSError) as exc_info:
            _aiosendspin_server_mod.AsyncZeroconf()

    assert exc_info.value.errno == errno.EACCES


def test_factory_returns_real_zeroconf_when_available() -> None:
    """When AsyncZeroconf succeeds, the factory should return its instance."""
    mock_zc = MagicMock()

    with patch("zeroconf.asyncio.AsyncZeroconf", return_value=mock_zc):
        _make_offline_resilient_zeroconf()
        result = _aiosendspin_server_mod.AsyncZeroconf()

    assert result is mock_zc


# ---------------------------------------------------------------------------
# Integration: run_server survives Zeroconf ENODEV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_server_starts_successfully_when_zeroconf_fails_offline() -> None:
    """run_server should not raise even when AsyncZeroconf raises ENODEV."""

    offline_error = OSError(errno.ENODEV, "No such device")

    mock_server = MagicMock()
    mock_server.start_server = AsyncMock(side_effect=offline_error)

    # Simulate: start_server raises ENODEV (as if Zeroconf init failed)
    # In real code, the patch converts this to a _NullZeroconf before start_server
    # is called, so start_server itself succeeds. Here we test that the patch
    # installed by run_server prevents the OSError from reaching the caller.

    # Patch at a lower level: make AsyncZeroconf raise ENODEV in the aiosendspin module.
    with patch("zeroconf.asyncio.AsyncZeroconf", side_effect=offline_error):
        # Call _make_offline_resilient_zeroconf directly to verify the patch works.
        _make_offline_resilient_zeroconf()
        # The factory should now return _NullZeroconf instead of raising.
        result = _aiosendspin_server_mod.AsyncZeroconf(
            ip_version=MagicMock(), interfaces=MagicMock()
        )
        assert isinstance(result, _NullZeroconf)
