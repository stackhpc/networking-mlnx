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

import threading
import unittest.mock as mock

from neutron.tests import base
from oslo_config import cfg
from oslo_serialization import jsonutils

from networking_mlnx.plugins.ml2.drivers.mlnx.agent import comm_utils
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import exceptions
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import utils

OK_REPLY = jsonutils.dumps({'status': 'OK', 'response': {'vnic': 'data'}})


class FakeReqSocket(object):
    """Enforces the send/receive alternation of a zmq REQ socket."""

    def __init__(self):
        self.awaiting_reply = False
        self.closed = False

    def send_string(self, msg):
        if self.awaiting_reply:
            raise RuntimeError('send_string called while awaiting a reply')
        self.awaiting_reply = True

    def recv_string(self):
        self.awaiting_reply = False
        return OK_REPLY

    def setsockopt(self, option, value):
        pass

    def connect(self, endpoint):
        pass

    def close(self):
        self.closed = True


class TestEswitchUtilsSendMsg(base.BaseTestCase):

    def setUp(self):
        super(TestEswitchUtilsSendMsg, self).setUp()
        self.zmq = mock.patch.object(utils, 'zmq').start()
        self.sockets = []
        self.zmq.Context.return_value.socket.side_effect = self._new_socket
        self.poller = self.zmq.Poller.return_value
        self.poller.poll.side_effect = self._poll
        # Retries must not sleep or outlive the zmq mock if a test fails.
        mock.patch.object(comm_utils.time, 'sleep').start()
        self.utils = utils.EswitchUtils('inproc://eswitchd-test', 3000)

    def _new_socket(self, socket_type):
        socket = FakeReqSocket()
        self.sockets.append(socket)
        return socket

    def _poll(self, timeout):
        return [(self.sockets[-1], self.zmq.POLLIN)]

    def test_send_msg(self):
        self.assertEqual({'vnic': 'data'}, self.utils.send_msg('msg'))
        self.assertEqual(1, len(self.sockets))
        self.assertFalse(self.utils._lock.locked())

    def test_send_msg_concurrent_requests_are_serialised(self):
        polling = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        errors = []

        def blocking_poll(timeout):
            polling.set()
            release.wait(5)
            return self._poll(timeout)

        def send():
            try:
                self.utils.send_msg('msg')
            except Exception as e:
                errors.append(e)

        self.poller.poll.side_effect = blocking_poll
        first = threading.Thread(target=send, daemon=True)
        first.start()
        self.assertTrue(polling.wait(5))
        # The first request is waiting for its reply. Without the lock, this
        # request would be sent on the same socket and fail.
        self.poller.poll.side_effect = self._poll
        second = threading.Thread(target=send, daemon=True)
        second.start()
        second.join(0.2)
        self.assertTrue(second.is_alive())
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual([], errors)

    def test_send_msg_timeout_resets_socket(self):
        self.poller.poll.side_effect = None
        self.poller.poll.return_value = []
        attempts = cfg.CONF.ESWITCH.retries + 1

        comm_utils.time.sleep.side_effect = (
            lambda interval: self.assertFalse(self.utils._lock.locked()))
        self.assertRaises(exceptions.RequestTimeout,
                          self.utils.send_msg, 'msg')

        self.assertEqual(attempts, len(self.sockets))
        self.assertTrue(all(socket.closed for socket in self.sockets))
        self.assertEqual(attempts, self.poller.unregister.call_count)
        self.assertFalse(self.utils._lock.locked())

    def test_send_msg_error_resets_socket(self):
        self.poller.poll.side_effect = ValueError('poll failed')

        self.assertRaises(ValueError, self.utils.send_msg, 'msg')
        self.assertTrue(self.sockets[0].closed)

        self.poller.poll.side_effect = self._poll
        self.assertEqual({'vnic': 'data'}, self.utils.send_msg('msg'))
        self.assertEqual(2, len(self.sockets))
