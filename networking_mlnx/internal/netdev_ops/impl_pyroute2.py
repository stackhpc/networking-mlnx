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
        with pyroute2.IPRoute() as ip:
            try:
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
        with pyroute2.IPRoute() as ip:
            try:
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
        with pyroute2.IPRoute() as ip:
            try:
                link_idx = ip.link_lookup(ifname=pf_ifname)[0]
                ip.link('set', index=link_idx, vf={'vf': int(vf_idx),
                                                'ib_port_guid': guid})
                ip.link('set', index=link_idx,
                        vf={'vf': int(vf_idx), 'ib_node_guid': guid})
            except IndexError:
                raise exceptions.NetworkInterfaceNotFound(pf_ifname)
            except pyroute2.NetlinkError as e:
                raise exceptions.NetlinkRuntimeError(e)

    def get_vf_guid(self, pf_ifname, vf_idx):
        """Get vf administrative GUID

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :returns vf_guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format
                          where x is a hexadecimal digit.

        NOTE: while there are two GUIDs assigned per VF (port and node GUID)
        we assume they are the same and return just one value.
        """
        with pyroute2.IPRoute() as ip:
            try:
                link_idx = ip.link_lookup(ifname=pf_ifname)[0]
                attrs = ip.link('get', index=link_idx, ext_mask=1)[0]
            except IndexError:
                raise exceptions.NetworkInterfaceNotFound(pf_ifname)
            except pyroute2.NetlinkError as e:
                raise exceptions.NetlinkRuntimeError(e)

        vf_attr = (attrs.get_attr('IFLA_VFINFO_LIST').
                   get_attrs("IFLA_VF_INFO"))[int(vf_idx)]
        node_guid_attr = vf_attr.get_attr("IFLA_VF_IB_NODE_GUID")
        port_guid_attr = vf_attr.get_attr("IFLA_VF_IB_PORT_GUID")

        if node_guid_attr is None or port_guid_attr is None:
            # Note(adrianc) both attributes are expected to be present
            raise exceptions.NetlinkAttrNotFoundError(
                "IFLA_VF_IB_NODE_GUID, IFLA_VF_IB_PORT_GUID")

        node_guid = node_guid_attr["ib_node_guid"]
        port_guid = port_guid_attr["ib_port_guid"]

        if node_guid != port_guid:
            # Note(adrianc) both attributes are expected to be the same
            raise exceptions.NetlinkUnexpectedAttrValue(
                "port and node GUID are expected to be the same for "
                "%(netdev)s-vf%(vf_idx)s. actual: %(node_guid)s, %(port_guid)s"
                % dict(netdev=pf_ifname,
                       vf_idx=str(vf_idx),
                       node_guid=node_guid,
                       port_guid=port_guid))
        return port_guid
