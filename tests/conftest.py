"""Tests never consult live copy journals or control a real transfer job."""
import pytest
import mcp_serial_hid_kvm.server as server
from mcp_serial_hid_kvm.file_copy import CopyManager

@pytest.fixture(autouse=True)
def isolated_copy_jobs(tmp_path,monkeypatch):
    manager=CopyManager(tmp_path/'copy-jobs')
    manager.interlocked=lambda:server._input_lock is not None
    monkeypatch.setattr(server,'_copies',manager)
    yield manager
    if manager.lease: manager.lease.close()
