# Copyright 2014 Mellanox Technologies, Ltd
#
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

from oslo_config import cfg
from oslo_config import fixture as fixture_config
import requests

from networking_mlnx.plugins.ml2.drivers.sdn import client
from networking_mlnx.plugins.ml2.drivers.sdn import config
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions
from networking_mlnx.tests import base


class TestClient(base.TestCase):

    def setUp(self):
        super(TestClient, self).setUp()
        self.conf_fixture = self.useFixture(fixture_config.Config())
        self.conf = self.conf_fixture.conf
        self._set_args()
        self.client = client.SdnRestClient.create_client()
        self.url = 'some_url'
        self.data = {'some': 'data'}

    def _set_args(self):
        self.conf.register_opts(config.sdn_opts, sdn_const.GROUP_OPT)
        for arg in client.SdnRestClient.MANDATORY_ARGS:
            self.conf.set_override(name=arg,
                                   override="http://some_val",
                                   group=sdn_const.GROUP_OPT)

    def test_mandatory_args(self):
        mandatory_arg_objects = filter(
                lambda obj: obj.name in client.SdnRestClient.MANDATORY_ARGS,
                config.sdn_opts)
        for arg in mandatory_arg_objects:
            self._set_args()
            self.conf.unregister_opt(opt=arg,
                                     group=sdn_const.GROUP_OPT)
            self.assertRaises(cfg.NoSuchOptError,
                              client.SdnRestClient.create_client)

    def test_cert_verify_default(self):
        test_client = client.SdnRestClient.create_client()
        self.assertEqual(False, test_client.verify)

    def test_cert_verify_true(self):
        self.conf_fixture.config(cert_verify=True,
                                 group=sdn_const.GROUP_OPT)
        test_client = client.SdnRestClient.create_client()
        self.assertEqual(True, test_client.verify)

    def test_cert_verify_false(self):
        self.conf_fixture.config(cert_verify=False,
                                 group=sdn_const.GROUP_OPT)
        test_client = client.SdnRestClient.create_client()
        self.assertEqual(False, test_client.verify)

    def test_cert_verify_cert_not_found(self):
        self.conf_fixture.config(cert_verify=True,
                                 group=sdn_const.GROUP_OPT)
        self.conf_fixture.config(cert_path="/Dummy.ca-bundle",
                                 group=sdn_const.GROUP_OPT)
        self.assertRaises(exceptions.SDNDriverCertError,
                          client.SdnRestClient.create_client)

    @mock.patch('networking_mlnx.plugins.ml2.drivers.'
                'sdn.client.SdnRestClient.request')
    def test_get(self, mocked_request):
        self.client.get(self.url, self.data)
        expected_url = '/'.join((self.conf.sdn.url, self.url))
        mocked_request.assert_called_once_with(sdn_const.GET,
                                               expected_url,
                                               self.data)

        mocked_request.reset_mock()
        self.client.get(self.url)
        mocked_request.assert_called_once_with(sdn_const.GET,
                                               expected_url,
                                               None)

        mocked_request.reset_mock()
        self.client.get()
        mocked_request.assert_called_once_with(sdn_const.GET,
                                               self.conf.sdn.url,
                                               None)

    @mock.patch('networking_mlnx.plugins.ml2.drivers.'
                'sdn.client.SdnRestClient.request')
    def test_put(self, mocked_request):
        self.client.put(self.url, self.data)
        expected_url = '/'.join((self.conf.sdn.url,
                                 self.conf.sdn.domain,
                                 self.url))
        mocked_request.assert_called_once_with(sdn_const.PUT,
                                               expected_url,
                                               self.data)

        mocked_request.reset_mock()
        self.client.put(self.url)
        mocked_request.assert_called_once_with(sdn_const.PUT,
                                               expected_url,
                                               None)

        mocked_request.reset_mock()
        self.client.put()
        expected_url = '/'.join((self.conf.sdn.url, self.conf.sdn.domain))
        mocked_request.assert_called_once_with(sdn_const.PUT,
                                               expected_url,
                                               None)

    @mock.patch('networking_mlnx.plugins.ml2.drivers.'
                'sdn.client.SdnRestClient.request')
    def test_post(self, mocked_request):
        self.client.post(self.url, self.data)
        expected_url = '/'.join((self.conf.sdn.url,
                                 self.conf.sdn.domain,
                                 self.url))
        mocked_request.assert_called_once_with(sdn_const.POST,
                                               expected_url,
                                               self.data)

        mocked_request.reset_mock()
        self.client.post(self.url)
        mocked_request.assert_called_once_with(sdn_const.POST,
                                               expected_url,
                                               None)

        mocked_request.reset_mock()
        self.client.post()
        expected_url = '/'.join((self.conf.sdn.url, self.conf.sdn.domain))
        mocked_request.assert_called_once_with(sdn_const.POST,
                                               expected_url,
                                               None)

    @mock.patch('networking_mlnx.plugins.ml2.drivers.'
                'sdn.client.SdnRestClient.request')
    def test_delete(self, mocked_request):
        self.client.delete(self.url, self.data)
        expected_url = '/'.join((self.conf.sdn.url,
                                 self.conf.sdn.domain,
                                 self.url))
        mocked_request.assert_called_once_with(sdn_const.DELETE,
                                               expected_url,
                                               self.data)

        mocked_request.reset_mock()
        self.client.delete(self.url)
        mocked_request.assert_called_once_with(sdn_const.DELETE,
                                               expected_url,
                                               None)

        mocked_request.reset_mock()
        self.client.delete()
        expected_url = '/'.join((self.conf.sdn.url, self.conf.sdn.domain))
        mocked_request.assert_called_once_with(sdn_const.DELETE,
                                               expected_url,
                                               None)

    def test_request_bad_data(self):
        # non serialized json data
        data = self
        self.assertRaises(ValueError,
                          self.client.request,
                          sdn_const.DELETE, '', data)

    @staticmethod
    def _response(status_code, text=''):
        response = requests.Response()
        response.status_code = status_code
        response._content = text.encode()
        response.url = 'http://some_val/ufmRestV3'
        return response

    def test_check_response_success(self):
        for status_code in (200, 201, 202, 204, 501):
            response = self._response(status_code)
            self.assertIs(response, self.client._check_response(
                response, sdn_const.POST))

    def test_check_response_delete_not_found(self):
        response = self._response(404)
        self.assertIs(response, self.client._check_response(
            response, sdn_const.DELETE))

    def test_check_response_rejected(self):
        for status_code in (400, 422):
            response = self._response(
                status_code, 'Missing attribute(s): client-id')
            e = self.assertRaises(exceptions.SDNRequestError,
                                  self.client._check_response,
                                  response, sdn_const.POST)
            self.assertIn('Missing attribute(s): client-id', str(e))

    def test_check_response_rejected_long_text(self):
        response = self._response(400, '<html>' + 'x' * 10000)
        e = self.assertRaises(exceptions.SDNRequestError,
                              self.client._check_response,
                              response, sdn_const.POST)
        self.assertLess(len(str(e)), 1000)

    def test_check_response_retryable(self):
        for status_code in (401, 403, 404, 409, 429, 500, 503):
            self.assertRaises(exceptions.SDNConnectionError,
                              self.client._check_response,
                              self._response(status_code), sdn_const.POST)
