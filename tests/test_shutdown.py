import fcntl
import threading
import time

import pytest
from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.config import Config

from apps.api.main import create_app


def test_shutdown_does_not_wait_for_blocked_worker_or_release_data_lock_early(tmp_path):
    started, release = threading.Event(), threading.Event()

    class Service:
        publisher = None
        storage = ArtifactStore(tmp_path)

        def recover(self):
            pass

        def process_next(self):
            started.set()
            assert release.wait(8)
            return False

    app = create_app(Config(tmp_path), ingestion_service=Service())
    try:
        with TestClient(app):
            assert started.wait(2)
        with (tmp_path / "instance.lock").open("a+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.set()
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
    finally:
        release.set()
