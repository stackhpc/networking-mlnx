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

from oslo_log import log as logging
import pyroute2

from networking_mlnx.internal.netdev_ops import exceptions
from networking_mlnx.internal.netdev_ops import netdev_ops_abs

LOG = logging.getLogger(__name__)


class IpCommand(netdev_ops_abs.NetDevOperations):
    """_IpCommand implements NetDevOperations interface using pyroute2

    """
    def set_vf_admin_state(self, pf_ifname, vf_idx, state):
        """Set vf admin state

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :param state: desired admin state as defined in
                      networking_mlnx.internal.netdev_ops.constants
        """
        try:
            ip = pyroute2.IPRoute()
            link_idx = ip.link_lookup(ifname=pf_ifname)[0]
            ip.link(
                'set', index=link_idx, vf={'vf': int(vf_idx),
                                           'link_state': state})
        except IndexError:
            raise exceptions.NetworkInterfaceNotFound(pf_ifname)
        except pyroute2.NetlinkError as e:
            raise exceptions.NetlinkRuntimeError(e)

    def set_link_state(self, ifname, state):
        """Set net device link state

        :param ifname: netdev name
        :param state: desired link state as defined in
                      networking_mlnx.internal.netdev_ops.constants
        """
        try:
            ip = pyroute2.IPRoute()
            link_idx = ip.link_lookup(ifname=ifname)[0]
            ip.link('set', index=link_idx, state=state)
        except IndexError:
            raise exceptions.NetworkInterfaceNotFound(ifname)
        except pyroute2.NetlinkError as e:
            raise exceptions.NetlinkRuntimeError(e)

    def set_vf_guid(self, pf_ifname, vf_idx, guid):
        """Set vf administrative port and node GUID

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :param guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format
                     where x is a hexadecimal digit.
        """
        try:
            ip = pyroute2.IPRoute()
            link_idx = ip.link_lookup(ifname=pf_ifname)[0]
            ip.link('set', index=link_idx, vf={'vf': int(vf_idx),
                                               'ib_port_guid': guid})
            ip.link('set', index=link_idx,
                    vf={'vf': int(vf_idx), 'ib_node_guid': guid})
        except IndexError:
            raise exceptions.NetworkInterfaceNotFound(pf_ifname)
        except pyroute2.NetlinkError as e:
            raise exceptions.NetlinkRuntimeError(e)
