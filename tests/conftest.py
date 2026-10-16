"""Shared fixtures, and a guard that proves the suite never leaves the machine: any socket
connection to anything other than 127.0.0.1 fails the test that tried it."""
from __future__ import annotations

import socket

import pytest

from bill_intake import rules as rules_mod
from bill_intake.samples import build_example_clinic

_real_connect = socket.socket.connect


def _loopback_only(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AssertionError(f"test tried to reach the network: {address!r}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _loopback_only)


@pytest.fixture
def clinic(tmp_path):
    """Example Clinic on disk; returns the loaded rules."""
    return rules_mod.load(build_example_clinic(tmp_path))


@pytest.fixture
def empty_clinic(tmp_path):
    """Example Clinic with its folders and rules but an empty intake."""
    return rules_mod.load(build_example_clinic(tmp_path, bills={}))
