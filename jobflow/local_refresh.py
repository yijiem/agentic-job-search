"""Consume small local refresh requests without delaying HTTP saves."""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


POLL_SECONDS = 2
REFRESH_TIMEOUT = 180


def request_stamp(root):
    """Read one durable marker without creating a database or scanning jobs."""
    path = Path(root) / 'data/jobflow.sqlite3'
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        row = connection.execute(
            "SELECT body FROM records WHERE kind='projection_requests' AND id='current'").fetchone()
        value = json.loads(row[0]) if row else {}
        stamp = value.get('requested_at') if isinstance(value, dict) else None
        return stamp if isinstance(stamp, str) and stamp else None
    finally:
        connection.close()


def _stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=1)


def run_refresh(root, stop):
    """Run the existing locked CLI, with bounded cancellation on server exit."""
    if stop.is_set():
        return False
    command = [sys.executable, '-m', 'jobflow', '--root', str(Path(root).resolve()), 'refresh']
    process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    try:
        while not stop.is_set() and time.monotonic() - started < REFRESH_TIMEOUT:
            try:
                unused_stdout, stderr = process.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
            # The normal CLI safely skips a held workflow lock with exit code
            # zero. That skip must not consume the pending refresh request.
            return process.returncode == 0 and 'Another workflow process is running' not in stderr
        return False
    finally:
        _stop_process(process)


def refresh_loop(root, stop):
    """Coalesce edits, retry busy work, and recover retained requests on start."""
    processed = None
    while not stop.is_set():
        try:
            requested = request_stamp(root)
            if requested and requested != processed and run_refresh(root, stop):
                # Remember only the version read before starting. An edit
                # committed during this refresh remains due on the next poll.
                processed = requested
        except (OSError, sqlite3.Error, ValueError, subprocess.TimeoutExpired):
            # Keep the durable marker intact. A transient error or a concurrent
            # edit cannot acknowledge work that did not complete successfully.
            pass
        stop.wait(POLL_SECONDS)
