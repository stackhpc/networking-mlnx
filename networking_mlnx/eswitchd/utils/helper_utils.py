#!/usr/bin/env python
# Copyright 2013 Mellanox Technologies, Ltd
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

from networking_mlnx.eswitchd.common import constants
from oslo_log import log as logging


LOG = logging.getLogger(__name__)


def set_conn_url(transport, addr, port):
    """Return connection string for using in ZMQ connect """
    return constants.CONN_URL % {'transport': transport,
                                 'port': port, 'addr': addr}
