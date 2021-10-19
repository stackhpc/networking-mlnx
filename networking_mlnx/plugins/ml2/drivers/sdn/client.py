# Copyright 2016 Mellanox Technologies, Ltd
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

from oslo_config import cfg
from oslo_log import log
from oslo_serialization import jsonutils
import requests

from networking_mlnx.plugins.ml2.drivers.sdn import config
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_exc
from networking_mlnx.plugins.ml2.drivers.sdn import utils as sdn_utils

LOG = log.getLogger(__name__)
cfg.CONF.register_opts(config.sdn_opts, sdn_const.GROUP_OPT)


class SdnRestClient(object):

    MANDATORY_ARGS = ('url', 'token')

    @classmethod
    def create_client(cls):
        return cls(
            cfg.CONF.sdn.url,
            cfg.CONF.sdn.domain,
            cfg.CONF.sdn.timeout,
            cfg.CONF.sdn.cert_verify,
            cfg.CONF.sdn.cert_path,
            cfg.CONF.sdn.token)

    def __init__(self, url, domain, timeout,
                 verify, cert_path, token):
        self.url = url
        self.domain = domain
        self.timeout = timeout
        self.token = token
        self._validate_mandatory_params_exist()
        self.url.rstrip("/")
        self.verify = verify
        self.headers = {"Authorization": "Basic {0}".format(self.token),
                        **sdn_const.JSON_HTTP_HEADER}
        if verify:
            self.verify = self._get_cert(cert_path)

    def _get_cert(self, cert_path):
        if cert_path:
            if os.path.exists(cert_path):
                return cert_path
            else:
                raise sdn_exc.SDNDriverCertError(
                    msg='certificate path: \"%s\" was not found' % cert_path)
        else:
            return True

    def _validate_mandatory_params_exist(self):
        for arg in self.MANDATORY_ARGS:
            if not getattr(self, arg):
                raise cfg.RequiredOptError(
                    arg, cfg.OptGroup(sdn_const.GROUP_OPT))

    def get(self, urlpath='', data=None):
        urlpath = sdn_utils.strings_to_url(self.url, urlpath)
        return self.request(sdn_const.GET, urlpath, data)

    def put(self, urlpath='', data=None):
        urlpath = sdn_utils.strings_to_url(self.url, self.domain, urlpath)
        return self.request(sdn_const.PUT, urlpath, data)

    def post(self, urlpath='', data=None):
        urlpath = sdn_utils.strings_to_url(self.url, self.domain, urlpath)
        return self.request(sdn_const.POST, urlpath, data)

    def delete(self, urlpath='', data=None):
        urlpath = sdn_utils.strings_to_url(self.url, self.domain, urlpath)
        return self.request(sdn_const.DELETE, urlpath, data)

    def request(self, method, urlpath='', data=None):
        data = jsonutils.dumps(data, indent=2) if data else None
        LOG.debug("Sending METHOD %(method)s URL %(url)s JSON %(data)s",
                  {'method': method, 'url': urlpath, 'data': data})

        return self._check_response(requests.request(
                method, url=str(urlpath), headers=self.headers,
                data=data, verify=self.verify, timeout=self.timeout), method)

    def _check_response(self, response, method):
        try:
            LOG.debug("request status: %d", response.status_code)
            request_found = True
            if response.text:
                LOG.debug("request text: %s", response.text)
            if (response.status_code == requests.codes.not_found and
                method == sdn_const.DELETE):
                request_found = False
            if (request_found and
                response.status_code != requests.codes.not_implemented):
                response.raise_for_status()
        except Exception as e:
            raise sdn_exc.SDNConnectionError(msg=e)
        return response
