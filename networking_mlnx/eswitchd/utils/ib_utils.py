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

import glob
import re

from oslo_log import log as logging

from networking_mlnx.eswitchd.common import constants
from networking_mlnx.internal.netdev_ops import api as net_dev_api
from networking_mlnx.internal.netdev_ops import constants as netdev_const
from networking_mlnx.internal.sys_ops import api as sys_api

LOG = logging.getLogger(__name__)


class IbUtils(object):
    INVALID_PKEY = 'none'
    DEFAULT_PKEY_IDX = 0
    PARTIAL_PKEY_IDX = 1
    DEFAULT_MASK = 0x7fff
    DEFAULT_PKEY = 0xffff
    PKEYS_PATH = "/sys/class/infiniband/%s/ports/%s/pkeys/*"

    def _config_vf_pkey(self, ppkey_idx, pkey_idx,
                        pf_mlx_dev, vf_pci_id, hca_port):
        path = constants.MLNX4_PKEY_INDEX_PATH % (pf_mlx_dev, vf_pci_id,
                                                  hca_port, pkey_idx)
        sys_api.sys_write(path, ppkey_idx)

    def _get_guid_idx_mlx4(self, pf_mlx_dev, dev, hca_port):
        path = constants.MLNX4_GUID_INDEX_PATH % (pf_mlx_dev, dev, hca_port)
        with open(path) as fd:
            idx = fd.readline().strip()
        return int(idx)

    def _get_guid_from_mac(self, mac, device_type):
        guid = None
        if device_type == constants.MLNX4_DEVICE_TYPE:
            if mac is None:
                guid = constants.MLNX4_INVALID_GUID
            else:
                mac = mac.replace(':', '')
                prefix = mac[:6]
                suffix = mac[6:]
                guid = prefix + '0000' + suffix
        elif (device_type == constants.MLNX5_DEVICE_TYPE):
            if mac is None:
                guid = constants.MLNX5_INVALID_GUID
            else:
                prefix = mac[:9]
                suffix = mac[9:]
                guid = prefix + '00:00:' + suffix
        return guid

    def get_vfs_macs_ib(self, pf_mlx_name, hca_port, vf_idxs, type):
        """Get assigned Infiniband mac address for VFs

        :param pf_mlx_name: PF IB device name
        :param hca_port: hca port number
        :param vf_idxs: list of VF indexes to get mac address
        :param type: PF device type, one of [constants.MLNX4_DEVICE_TYPE,
                    constants.MLNX5_DEVICE_TYPE]
        :return: mapping between VF index and mac
        """
        macs_map = {}
        if type == constants.MLNX4_DEVICE_TYPE:
            macs_map.update(
                self._get_vfs_macs_ib_mlnx4(pf_mlx_name, hca_port, vf_idxs))
        elif type == constants.MLNX5_DEVICE_TYPE:
            macs_map.update(
                self._get_vfs_macs_ib_mlnx5(pf_mlx_name, vf_idxs))
        return macs_map

    def _get_vfs_macs_ib_mlnx4(self, pf_mlx_name, hca_port, vf_idxs):
        macs_map = {}
        guids_path = constants.MLNX4_ADMIN_GUID_PATH % (pf_mlx_name, hca_port,
                                                  '[1-9]*')
        paths = glob.glob(guids_path)
        for path in paths:
            vf_index = path.split('/')[-1]
            if vf_index not in vf_idxs:
                continue
            with open(path) as f:
                guid = f.readline().strip()
                if guid == constants.MLNX4_INVALID_GUID:
                    mac = constants.INVALID_MAC
                else:
                    head = guid[:6]
                    tail = guid[-6:]
                    mac = ":".join(re.findall('..?', head + tail))
                macs_map[str(int(vf_index))] = mac
        return macs_map

    def _get_vfs_macs_ib_mlnx5(self, pf_mlx_name, vf_idxs):
        macs_map = {}
        for vf_idx in vf_idxs:
            guid_path = (
                constants.MLNX5_GUID_NODE_PATH % {'module': pf_mlx_name,
                                                  'vf_num': vf_idx})
            with open(guid_path) as f:
                guid = f.readline().strip()
                head = guid[:8]
                tail = guid[-9:]
                mac = head + tail
            macs_map[vf_idx] = mac
        return macs_map

    def config_vf_mac_address(self, pf_net_dev, pf_mlx_dev, vf_idx,
                              vf_pci_slot, vf_device_type, hca_port=None,
                              mac=None):
        """Configure VF infiniband MAC address

        :param pf_net_dev: PF net device name
        :param pf_mlx_dev: PF mlx device name
        :param vf_idx: VF index
        :param vf_pci_slot: VF PCI slot
        :param vf_device_type: VF device type, one of
                               (constants.MLNX4_DEVICE_TYPE,
                               constants.MLNX5_DEVICE_TYPE)
        :param hca_port: HCA port number (ConnectX3 family only)
        :param mac: mac address
        """
        vguid = self._get_guid_from_mac(mac, vf_device_type)
        if vf_device_type == constants.MLNX4_DEVICE_TYPE:
            self._config_vf_mac_address_mlnx4(pf_net_dev, pf_mlx_dev, vf_idx,
                                              vf_pci_slot, hca_port, vguid)
        elif (vf_device_type == constants.MLNX5_DEVICE_TYPE):
            self._config_vf_mac_address_mlnx5(pf_net_dev, pf_mlx_dev, vf_idx,
                                              vf_pci_slot, vguid)
        else:
            LOG.error("Unsupported vf device type: %s ", vf_device_type)

    def _config_vf_mac_address_mlnx4(self, pf_net_dev, pf_mlx_dev, vf_idx,
                                     vf_pci_slot, hca_port, vguid):
        self._config_vf_pkey(
            IbUtils.INVALID_PKEY, IbUtils.DEFAULT_PKEY_IDX, pf_mlx_dev,
            vf_pci_slot, hca_port)

        try:
            net_dev_api.set_vf_guid(pf_net_dev, vf_idx, vguid)
        except Exception as e:
            LOG.info("Failed to set vf guid via netlink. "
                     "%s, attempting to set vf guid via sysfs", str(e))
            guid_idx = self._get_guid_idx_mlx4(pf_mlx_dev, vf_pci_slot,
                                               hca_port)
            path = constants.MLNX4_ADMIN_GUID_PATH % (
                pf_mlx_dev, hca_port, guid_idx)
            sys_api.sys_write(path, vguid)

        ppkey_idx = self._get_pkey_idx(
            IbUtils.DEFAULT_PKEY, pf_mlx_dev, hca_port)
        if ppkey_idx is not None:
            self._config_vf_pkey(
                ppkey_idx, IbUtils.PARTIAL_PKEY_IDX, pf_mlx_dev, vf_pci_slot,
                hca_port)
        else:
            LOG.error("Can't find partial management pkey for "
                      "%(pf)s:%(dev)s", {'pf': pf_mlx_dev, 'dev': vf_pci_slot})

    def _config_vf_mac_address_mlnx5(self, pf_net_dev, pf_mlx_dev, vf_idx,
                                     vf_pci_slot, vguid):
        try:
            net_dev_api.set_vf_guid(pf_net_dev, vf_idx, vguid)
        except Exception as e:
            LOG.info("Failed to set vf guid via netlink. "
                     "%s, attempting to set vf guid via sysfs", str(e))
            guid_node = constants.MLNX5_GUID_NODE_PATH % {'module': pf_mlx_dev,
                                                          'vf_num': vf_idx}
            guid_port = constants.MLNX5_GUID_PORT_PATH % {'module': pf_mlx_dev,
                                                          'vf_num': vf_idx}
            for path in (guid_node, guid_port):
                sys_api.sys_write(path, vguid)

        if vguid == constants.MLNX5_INVALID_GUID:
            net_dev_api.set_vf_admin_state(
                pf_net_dev, vf_idx, netdev_const.ADMIN_STATE_DOWN)
            sys_api.sys_write(constants.UNBIND_PATH, vf_pci_slot)
            sys_api.sys_write(constants.BIND_PATH, vf_pci_slot)

    def config_vlan_ib(self, pf_mlx_dev, vf_pci_slot, vf_device_type, hca_port,
                       vlan):
        if vf_device_type == constants.MLNX4_DEVICE_TYPE:
            self._config_vlan_ib_mlnx4(pf_mlx_dev, vf_pci_slot, hca_port, vlan)
        elif vf_device_type == constants.MLNX5_DEVICE_TYPE:
            pass
        else:
            LOG.error("Unsupported vf device type: %s ", vf_device_type)

    def _config_vlan_ib_mlnx4(self, pf_mlx_dev, vf_pci_slot, hca_port, vlan):
        if vlan == 0:
            ppkey_idx = self._get_pkey_idx(
                IbUtils.DEFAULT_PKEY, pf_mlx_dev, hca_port)
            if ppkey_idx is not None:
                self._config_vf_pkey(
                    ppkey_idx, IbUtils.DEFAULT_PKEY_IDX, pf_mlx_dev,
                    vf_pci_slot, hca_port)
        else:
            ppkey_idx = self._get_pkey_idx(vlan, pf_mlx_dev, hca_port)
            if ppkey_idx is not None:
                self._config_vf_pkey(
                    ppkey_idx, IbUtils.DEFAULT_PKEY_IDX, pf_mlx_dev,
                    vf_pci_slot, hca_port)

    def _get_pkey_idx(self, vlan, pf_mlx_dev, hca_port):
        """get PKEY index for provided vlan ID

        :param vlan: Vlan ID
        :param pf_mlx_dev: PF mlx device
        :param hca_port: HCA port number
        :return: matching pkey index or None
        """
        paths = IbUtils.PKEYS_PATH % (pf_mlx_dev, hca_port)
        for path in glob.glob(paths):
            fd = open(path)
            pkey = fd.readline()
            fd.close()
            # the MSB in pkey is the membership bit ( 0 - partial, 1 - full)
            # the other 15 bit are the number of the pkey
            # so we want to remove the 16th bit when compare pkey file
            # to the vlan (pkey) we are looking for
            is_match = (int(pkey, 16) & IbUtils.DEFAULT_MASK == vlan &
                        IbUtils.DEFAULT_MASK)
            if is_match:
                return int(path.split('/')[-1])
        return None
