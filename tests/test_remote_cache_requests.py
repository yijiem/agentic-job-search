"""Concurrent HTTP reads cannot revert unpublished cache writes."""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from jobflow.cloud import CloudHandler, CloudServer


class CacheRequestTests(unittest.TestCase):
    def test_get_waits_for_post_to_publish_before_hydrating_shared_cache(self):
        staged = threading.Event()
        publish = threading.Event()
        get_entered = threading.Event()
        get_hydrated = threading.Event()
        account = {'id': 'owner'}
        remote = {'value': 'old'}
        outcomes = {}
        errors = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'profile.json'
            target.write_text('old')

            class Registry:
                def workspace(self, requested):
                    return root

            def hydrate(path):
                # Match ensure_file's authoritative version restoration.
                target.write_text(remote['value'])
                if staged.is_set():
                    get_hydrated.set()

            class Handler(CloudHandler):
                def do_POST(self):
                    self._root(account)
                    target.write_text('new')
                    staged.set()
                    if not publish.wait(5):
                        self._respond(500, {'error': 'test did not publish'})
                        return
                    remote['value'] = target.read_text()
                    self._respond(200, {'value': remote['value']})

                def do_GET(self):
                    get_entered.set()
                    self._root(account)
                    self._respond(200, {'value': target.read_text()})

            server = CloudServer(('127.0.0.1', 0), root / 'server',
                                 'http://127.0.0.1', auth=object())
            server.accounts = Registry()
            server.RequestHandlerClass = Handler
            serving = threading.Thread(target=server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)

            def request(method):
                connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
                try:
                    connection.request(method, '/')
                    response = connection.getresponse()
                    outcomes[method] = (response.status, json.loads(response.read()))
                except BaseException as error:
                    errors.append(error)
                finally:
                    connection.close()

            with patch('jobflow.cloud.remote_enabled', return_value=True), \
                    patch('jobflow.cloud.hydrate_workspace', side_effect=hydrate):
                serving.start()
                writer = threading.Thread(target=request, args=('POST',))
                reader = threading.Thread(target=request, args=('GET',))
                try:
                    writer.start()
                    self.assertTrue(staged.wait(2))
                    reader.start()
                    self.assertTrue(get_entered.wait(2))
                    self.assertFalse(get_hydrated.wait(0.1), 'GET hydrated a cache with an unpublished POST write')
                    publish.set()
                    writer.join(3)
                    reader.join(3)
                    self.assertFalse(errors)
                    self.assertEqual(outcomes['POST'], (200, {'value': 'new'}))
                    self.assertEqual(outcomes['GET'], (200, {'value': 'new'}))
                    self.assertEqual(remote['value'], 'new')
                finally:
                    publish.set()
                    server.shutdown()
                    server.server_close()
                    serving.join(2)
                    writer.join(2)
                    if reader.ident:
                        reader.join(2)


if __name__ == '__main__':
    unittest.main()
