# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest.mock as mock

from oslo_serialization import jsonutils

from networking_mlnx.eswitchd import eswitch_daemon
from networking_mlnx.tests import base


class TestHandleMsg(base.TestCase):

    def setUp(self):
        super(TestHandleMsg, self).setUp()
        # Bypass __init__, which needs the eswitch hardware and config.
        self.daemon = eswitch_daemon.MlxEswitchDaemon.__new__(
            eswitch_daemon.MlxEswitchDaemon)
        self.daemon.socket_os = mock.Mock()
        self.daemon.dispatcher = mock.Mock()

    def _handle(self, msg):
        self.daemon.socket_os.recv.return_value = msg
        self.daemon._handle_msg()
        self.daemon.socket_os.send_string.assert_called_once()
        return jsonutils.loads(
            self.daemon.socket_os.send_string.call_args[0][0])

    def _assert_fails(self, msg, reason):
        reply = self._handle(msg)
        self.assertEqual({'status': 'FAIL', 'action': 'UNKNOWN',
                          'reason': reason}, reply)
        self.daemon.dispatcher.handle_msg.assert_not_called()

    def test_valid_message(self):
        self.daemon.dispatcher.handle_msg.return_value = {
            'status': 'OK', 'action': 'get_vnics', 'response': {}}
        reply = self._handle(b'{"action": "get_vnics", "fabric": "*"}')
        self.assertEqual('OK', reply['status'])
        self.daemon.dispatcher.handle_msg.assert_called_once_with(
            {'action': 'get_vnics', 'fabric': '*'})

    def test_dispatch_error(self):
        self.daemon.dispatcher.handle_msg.side_effect = Exception('boom')
        reply = self._handle(b'{"action": "get_vnics"}')
        self.assertEqual({'status': 'FAIL', 'action': 'get_vnics',
                          'reason': 'boom'}, reply)

    def test_not_json(self):
        self._assert_fails(b'GET / HTTP/1.1\r\n\r\n', 'Malformed message')

    def test_empty(self):
        self._assert_fails(b'', 'Malformed message')

    def test_not_utf8(self):
        self._assert_fails(b'\xff\xfe', 'Malformed message')

    def test_too_deeply_nested(self):
        self._assert_fails(b'[' * 200000, 'Malformed message')

    def test_not_object(self):
        self._assert_fails(b'["get_vnics"]',
                           'Message is not a JSON object')

    def test_null(self):
        self._assert_fails(b'null', 'Message is not a JSON object')
