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

from networking_mlnx.internal.netdev_ops import impl_pyroute2
from networking_mlnx import privsep

# for now we have only one implementation
__netdev_ops = impl_pyroute2.IpCommand()


@privsep.admin_pctx.entrypoint
def set_vf_admin_state(pf_ifname, vf_idx, state):
    """Set vf admin state

    :param pf_ifname: pf netdev name
    :param vf_idx: vf index
    :param state: desired admin state as defined in
                  networking_mlnx.internal.netdev_ops.constants
    """
    __netdev_ops.set_vf_admin_state(pf_ifname, vf_idx, state)


@privsep.admin_pctx.entrypoint
def set_link_state(ifname, state):
    """Set net device link state

    :param ifname: netdev name
    :param state: desired link state as defined in
                  networking_mlnx.internal.netdev_ops.constants
    """
    __netdev_ops.set_link_state(ifname, state)


@privsep.admin_pctx.entrypoint
def set_vf_guid(pf_ifname, vf_idx, guid):
    """Set vf administrative port and node GUID

    :param pf_ifname: pf netdev name
    :param vf_idx: vf index
    :param guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format
                 where x is a hexadecimal digit.
    """
    __netdev_ops.set_vf_guid(pf_ifname, vf_idx, guid)


def get_vf_guid(pf_ifname, vf_idx):
    """Get vf administrative GUID

    :param pf_ifname: pf netdev name
    :param vf_idx: vf index
    :returns vf_guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format where
                      x is a hexadecimal digit.

    NOTE: This operation does not need elevated privileges.
    """
    return __netdev_ops.get_vf_guid(pf_ifname, vf_idx)
