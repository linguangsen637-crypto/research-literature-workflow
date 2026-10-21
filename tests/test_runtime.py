import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from research_os.core import SafetyError
from research_os.zotero import SandboxLoopbackProxy, zotero_opener
from research_preflight import ensure_zotero


class RuntimeTests(unittest.TestCase):
    def test_codex_proxy_routes_loopback_despite_no_proxy(self):
        handler = SandboxLoopbackProxy('http://127.0.0.1:3128')
        request = urllib.request.Request('http://127.0.0.1:23119/api/users/1/items')
        with patch.dict(os.environ, {'NO_PROXY': '*'}):
            handler.proxy_open(request, 'http://127.0.0.1:3128', 'http')
        self.assertEqual(request.host, '127.0.0.1:3128')
        self.assertEqual(request.selector, 'http://127.0.0.1:23119/api/users/1/items')

    def test_proxy_cannot_route_another_destination(self):
        handler = SandboxLoopbackProxy('http://127.0.0.1:3128')
        for url in ['http://127.0.0.1:7897/api/', 'http://example.com/api/',
                    'http://localhost:23119/api/']:
            with self.subTest(url=url), self.assertRaises(SafetyError):
                handler.proxy_open(urllib.request.Request(url), '', 'http')

    def test_invalid_proxy_refused(self):
        for url in ['http://example.com:3128', 'http://user:secret@127.0.0.1:3128',
                    'http://127.0.0.1:3128/route', 'https://127.0.0.1:3128']:
            with self.subTest(url=url), self.assertRaises(SafetyError):
                SandboxLoopbackProxy(url)

    def test_active_proxy_does_not_fall_back_to_direct(self):
        with patch.dict(os.environ, {'CODEX_NETWORK_PROXY_ACTIVE': '1'}, clear=True):
            with self.assertRaises(SafetyError):
                zotero_opener()

    def test_host_ignores_upstream_proxy(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://example.com:1234'}, clear=True):
            opener = zotero_opener()
        self.assertFalse(any(isinstance(h, SandboxLoopbackProxy) for h in opener.handlers))

    def test_identity_failure_does_not_start_zotero(self):
        with patch('research_preflight.connection', side_effect=SafetyError('collection/library identity mismatch')):
            with patch('research_preflight.subprocess.Popen') as launch:
                with self.assertRaises(SafetyError):
                    ensure_zotero()
                launch.assert_not_called()

    def test_healthy_service_is_not_restarted(self):
        with patch('research_preflight.connection', return_value={'status': 'passed'}):
            with patch('research_preflight.subprocess.Popen') as launch:
                self.assertEqual(ensure_zotero(), {'status': 'passed'})
                launch.assert_not_called()
