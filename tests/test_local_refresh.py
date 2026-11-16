import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jobflow import local_refresh


class PollStop:
    def __init__(self, iterations):
        self.remaining = iterations

    def is_set(self):
        return self.remaining <= 0

    def wait(self, seconds):
        self.remaining -= 1


class LocalRefreshTests(unittest.TestCase):
    def test_marker_read_is_read_only_and_missing_workspace_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(local_refresh.request_stamp(root))
            self.assertEqual([], list(root.iterdir()))
            (root / 'data').mkdir()
            db = sqlite3.connect(root / 'data/jobflow.sqlite3')
            db.execute('CREATE TABLE records(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id))')
            body = json.dumps({'id': 'current', 'requested_at': '2026-11-15T12:00:00+00:00'})
            db.execute('INSERT INTO records VALUES(?,?,?)', ('projection_requests', 'current', body))
            db.commit()
            self.assertEqual('2026-11-15T12:00:00+00:00', local_refresh.request_stamp(root))
            self.assertEqual(body, db.execute('SELECT body FROM records').fetchone()[0])
            db.close()

    def test_retained_request_runs_on_start_and_success_is_coalesced(self):
        stop = PollStop(3)
        with patch.object(local_refresh, 'request_stamp', return_value='request-1'), \
                patch.object(local_refresh, 'run_refresh', return_value=True) as run:
            local_refresh.refresh_loop('/unused', stop)
        run.assert_called_once_with('/unused', stop)

    def test_busy_or_failed_refresh_retries_the_same_request(self):
        stop = PollStop(3)
        with patch.object(local_refresh, 'request_stamp', return_value='request-1'), \
                patch.object(local_refresh, 'run_refresh', side_effect=[False, True]) as run:
            local_refresh.refresh_loop('/unused', stop)
        self.assertEqual(2, run.call_count)

    def test_edit_arriving_during_refresh_remains_due(self):
        stop = PollStop(3)
        with patch.object(local_refresh, 'request_stamp', side_effect=['before', 'during', 'during']), \
                patch.object(local_refresh, 'run_refresh', return_value=True) as run:
            local_refresh.refresh_loop('/unused', stop)
        self.assertEqual(2, run.call_count)

    def test_sqlite_error_does_not_consume_a_pending_request(self):
        stop = PollStop(2)
        with patch.object(local_refresh, 'request_stamp', side_effect=[sqlite3.OperationalError('busy'), 'request']), \
                patch.object(local_refresh, 'run_refresh', return_value=True) as run:
            local_refresh.refresh_loop('/unused', stop)
        run.assert_called_once()

    def test_cli_lock_skip_is_not_acknowledged_as_success(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ('', 'Another workflow process is running; try again later.')
        process.poll.return_value = 0
        with patch.object(local_refresh.subprocess, 'Popen', return_value=process) as popen:
            self.assertFalse(local_refresh.run_refresh('/workspace', threading.Event()))
        command = popen.call_args.args[0]
        self.assertEqual('refresh', command[-1])
        self.assertEqual(['-m', 'jobflow', '--root'], command[1:4])
        self.assertNotIn('shell', popen.call_args.kwargs)
        process.terminate.assert_not_called()

    def test_server_shutdown_terminates_in_progress_refresh(self):
        stop = threading.Event()
        process = Mock(returncode=None)
        process.poll.return_value = None

        def communicate(timeout):
            if not stop.is_set():
                stop.set()
                raise subprocess.TimeoutExpired('refresh', timeout)
            return '', ''

        process.communicate.side_effect = communicate
        with patch.object(local_refresh.subprocess, 'Popen', return_value=process):
            self.assertFalse(local_refresh.run_refresh('/workspace', stop))
        process.terminate.assert_called_once()
        process.kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
