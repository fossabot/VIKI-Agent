"""Wake acknowledgement proves admission, not a native page or fragment delivery."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from viki_agent import __main__ as launcher


@pytest.mark.parametrize("outcome", ["ack", "timeout", "busy", "old_reply", "error"])
def test_wake_requires_owned_reply_and_cleans_every_exit(monkeypatch, outcome):
    request, reply, old_reply, caller = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(launcher, "OPENED_EVENT", "test-reply", raising=False)
    monkeypatch.setattr(launcher, "WAKE_MUTEX", "test-caller", raising=False)
    monkeypatch.setattr(launcher, "WAKE_WAIT_S", 0.05)
    monkeypatch.setattr(
        launcher.winproc, "single_instance", lambda name: None if outcome == "busy" else caller
    )

    def open_signal(name):
        if name == launcher.OPEN_EVENT:
            return request
        assert name == launcher.OPENED_EVENT
        return old_reply if outcome == "old_reply" else None

    monkeypatch.setattr(launcher.winproc.Signal, "open", open_signal)
    create = Mock(return_value=reply)
    monkeypatch.setattr(launcher.winproc.Signal, "create", create)
    reply.wait.side_effect = [
        False,
        OSError("reply failed") if outcome == "error" else outcome == "ack",
    ]
    try:
        if outcome == "error":
            with pytest.raises(OSError, match="reply failed"):
                launcher._wake_running_instance()
        else:
            assert launcher._wake_running_instance() is (outcome == "ack")
    finally:
        request.close.assert_called_once()
        request.wait.assert_not_called()  # POSIX openers cannot wait.
        if outcome != "busy":
            caller.release.assert_called_once()
    if outcome in {"busy", "old_reply"}:
        create.assert_not_called()
        request.set.assert_not_called()
        reply.close.assert_not_called()  # Never close another creator's reply.
        if outcome == "old_reply":
            old_reply.close.assert_called_once()  # Only our probe handle.
    else:
        create.assert_called_once_with(launcher.OPENED_EVENT)
        request.set.assert_called_once()
        assert reply.wait.call_args_list[0].args == (0,)  # Drain an old Windows event bit.
        assert reply.wait.call_args_list[1].args == (50,)
        reply.close.assert_called_once()


def test_lock_handoff_is_bounded_without_sleeping(monkeypatch):
    clock = [0.0]
    attempts = []
    lock = Mock()
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        launcher.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def acquire(name):
        attempts.append(clock[0])
        return lock if clock[0] >= 0.02 else None

    monkeypatch.setattr(launcher.winproc, "single_instance", acquire)
    assert launcher._lock_when_free("test-server", 0.05, poll_s=0.01) is lock
    assert attempts == [0.01, 0.02]
    monkeypatch.setattr(launcher.winproc, "single_instance", lambda name: None)
    assert launcher._lock_when_free("test-server", 0.03, poll_s=0.01) is None
    assert clock[0] == pytest.approx(0.05)
