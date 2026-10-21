"""Ledger writes protect existing files and remain best-effort."""

import errno
import json
import os
import stat

import pytest

from lcc.relevance.jev import JevClient


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file permissions required")
def test_existing_ledger_permissions_are_tightened_without_losing_entries(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    previous = {"error": "previous request"}
    ledger.write_text(json.dumps(previous) + "\n", encoding="utf-8")
    ledger.chmod(0o644)
    client = JevClient(api_key="test-key", ledger_path=ledger)
    entry = {"error": "new request"}

    client._ledger_append(entry)

    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert [json.loads(line) for line in ledger.read_text().splitlines()] == [previous, entry]


def test_ledger_write_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    ledger = tmp_path / "ledger.jsonl"
    client = JevClient(api_key="test-key", ledger_path=ledger)
    entry = {"error": "request failed"}

    client._ledger_append(entry)

    assert json.loads(ledger.read_text()) == entry


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file permissions required")
def test_chmod_failure_skips_write_and_closes_descriptor(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    descriptors = []

    def fail_chmod(fd, mode):
        descriptors.append(fd)
        raise OSError(errno.EPERM, "Permission denied")

    monkeypatch.setattr(os, "fchmod", fail_chmod)
    client = JevClient(api_key="test-key", ledger_path=ledger)

    client._ledger_append({"error": "sensitive response"})

    assert ledger.read_text() == ""
    assert len(descriptors) == 1
    with pytest.raises(OSError) as exc:
        os.fstat(descriptors[0])
    assert exc.value.errno == errno.EBADF
