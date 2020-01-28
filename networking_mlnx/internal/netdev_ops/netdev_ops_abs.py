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

import abc

import six


@six.add_metaclass(abc.ABCMeta)
class NetDevOperations(object):

    @abc.abstractmethod
    def set_vf_admin_state(self, pf_ifname, vf_idx, state):
        """Set vf admin state

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :param state: desired admin state as defined in
                      networking_mlnx.internal.netdev_ops.constants
        """
        pass

    @abc.abstractmethod
    def set_link_state(self, ifname, state):
        """Set net device link state

        :param ifname: netdev name
        :param state: desired link state as defined in
                      networking_mlnx.internal.netdev_ops.constants
        """

    @abc.abstractmethod
    def set_vf_guid(self, pf_ifname, vf_idx, guid):
        """Set vf administrative port and node GUID

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :param guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format
                     where x is a hexadecimal digit.
        """

    @abc.abstractmethod
    def get_vf_guid(self, pf_ifname, vf_idx):
        """Get vf administrative GUID

        :param pf_ifname: pf netdev name
        :param vf_idx: vf index
        :returns guid: 64bit guid str in xx:xx:xx:xx:xx:xx:xx:xx format
                          where x is a hexadecimal digit.
        """
