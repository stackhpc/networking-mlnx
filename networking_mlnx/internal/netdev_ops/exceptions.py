# Copyright 2019 Mellanox Technologies, Ltd
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

from networking_mlnx._i18n import _


class NetworkInterfaceNotFound(RuntimeError):
    message = _("Network interface %(device)s not found")

    def __init__(self, message=None, device=None):
        message = message or self.message % {'device': device}
        super(NetworkInterfaceNotFound, self).__init__(message)


class NetlinkRuntimeError(RuntimeError):
    message = _("Failed to execute Netlink command. %(e)s")

    def __init__(self, e):
        message = self.message % {'e': str(e)}
        super(NetlinkRuntimeError, self).__init__(message)


class NetlinkAttrNotFoundError(RuntimeError):
    message = _("Required netlink attribute was not found. %(e)s")

    def __init__(self, e):
        message = self.message % {'e': str(e)}
        super(NetlinkAttrNotFoundError, self).__init__(message)


class NetlinkUnexpectedAttrValue(RuntimeError):
    message = _("Unexpected Netlink attribute value. %(e)s")

    def __init__(self, e):
        message = self.message % {'e': str(e)}
        super(NetlinkUnexpectedAttrValue, self).__init__(message)
