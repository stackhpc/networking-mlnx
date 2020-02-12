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
from networking_mlnx.eswitchd.common import exceptions
from networking_mlnx.eswitchd.utils import pci_utils
from networking_mlnx.internal.netdev_ops import api as net_dev_api
from networking_mlnx.internal.netdev_ops import constants as netdev_const
from networking_mlnx.internal.netdev_ops import exceptions as api_exceptions
from networking_mlnx.internal.sys_ops import api as sys_api

LOG = logging.getLogger(__name__)


class IbUtils(object):
    INVALID_PKEY = 'none'
    DEFAULT_PKEY_IDX = 0
    PARTIAL_PKEY_IDX = 1
    DEFAULT_MASK = 0x7fff
    DEFAULT_PKEY = 0xffff
    PKEYS_PATH = "/sys/class/infiniband/%s/ports/%s/pkeys/*"
    GUID_FMT_MLNX4 = r"^[0-9a-fA-F]{16}$"
    GUID_FMT_MLNX5 = r"^([0-9a-fA-F]{2}:){7}([0-9A-Fa-f]{2})$"

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

    def _get_mac_from_guid(self, guid):
        """Truncate GUID to mac by removing the 4th and 5th bytes.

        :param guid: str formatted in either: xxxxxxxxxxxxxxxx or
                     xx:xx:xx:xx:xx:xx:xx:xx where 'x' is a hexadecimal digit
        :return: mac
        """
        if re.match(IbUtils.GUID_FMT_MLNX4, guid):
            mac = ":".join(re.findall('..?', guid[:6] + guid[-6:]))
        elif re.match(IbUtils.GUID_FMT_MLNX5, guid):
            mac = guid[:8] + guid[-9:]
        else:
            raise exceptions.InvalidGUIDFormatException(guid)
        return mac

    def get_vfs_macs_ib(self, pf_net_name, pf_mlx_name, hca_port, vf_idxs,
                        type):
        """Get assigned Infiniband mac address for VFs

        :param pf_net_name: PF net device name
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
                self._get_vfs_macs_ib_mlnx4(pf_net_name, pf_mlx_name,
                                            hca_port, vf_idxs))
        elif type == constants.MLNX5_DEVICE_TYPE:
            macs_map.update(
                self._get_vfs_macs_ib_mlnx5(pf_net_name, pf_mlx_name,
                                            vf_idxs))
        return macs_map

    def _get_gid_to_vf_idx_mapping(self, pf_ib_dev, hca_port, vf_idxs):
        """Get mapping between VF GID index and VF PCI index

        :param pf_ib_dev: PF IB device name
        :param hca_port: hca port number
        :param vf_idxs: list of vf indexes
        :return: dict mapping between gid index and VF PCI index
        """
        pciu = pci_utils.pciUtils()
        pf_pci_addr = pciu.get_pci_from_ib_dev(pf_ib_dev)
        mapping = {}
        for vf_idx in vf_idxs:
            vf_pci_addr = pciu.get_vf_from_vf_idx(pf_pci_addr, vf_idx)
            vf_gid = self._get_guid_idx_mlx4(pf_ib_dev, vf_pci_addr, hca_port)
            mapping[vf_gid] = vf_idx
        return mapping

    def _get_vfs_ib_mac_netdev_api(self, pf_net_name, vf_idxs):
        try:
            macs_map = {}
            for vf_idx in vf_idxs:
                guid = net_dev_api.get_vf_guid(pf_net_name, vf_idx)
                macs_map[vf_idx] = self._get_mac_from_guid(guid)
            return macs_map
        except api_exceptions.NetlinkAttrNotFoundError:
            return None

    def _get_vfs_macs_ib_mlnx4(self, pf_net_name, pf_mlx_name, hca_port,
                               vf_idxs):
        # NOTE(adrianc): In contrast to MLNX5 devices, it is not expected
        # that MLNX4 devices will support GET vf GUIDs via netlink, hence we
        # do not attempt to get them via netdev API (netlink impl).
        macs_map = {}
        gid_idx_to_vf_idx = self._get_gid_to_vf_idx_mapping(
            pf_mlx_name, hca_port, vf_idxs)
        gid_idxs = gid_idx_to_vf_idx.keys()
        guids_path = constants.MLNX4_ADMIN_GUID_PATH % (pf_mlx_name,
                                                        hca_port,
                                                        '[1-9]*')
        paths = glob.glob(guids_path)
        for path in paths:
            gid_index = int(path.split('/')[-1])
            if gid_index not in gid_idxs:
                continue
            with open(path) as f:
                guid = f.readline().strip()
                if guid == constants.MLNX4_INVALID_GUID:
                    mac = constants.INVALID_MAC
                else:
                    mac = self._get_mac_from_guid(guid)
                macs_map[gid_idx_to_vf_idx[gid_index]] = mac
        return macs_map

    def _get_vfs_macs_ib_mlnx5(self, pf_net_name, pf_mlx_name, vf_idxs):
        macs_map = self._get_vfs_ib_mac_netdev_api(pf_net_name, vf_idxs)
        # TODO(adrianc): The logic below should be removed once major distros
        # have kernel based on 5.5.0 or newer.
        if macs_map is None:
            LOG.debug("Failed to get vf guid via netdev API, "
                     "attempting to get vf guid via sysfs.")
            macs_map = {}
            for vf_idx in vf_idxs:
                guid_path = (
                    constants.MLNX5_GUID_NODE_PATH % {'module': pf_mlx_name,
                                                      'vf_num': int(vf_idx)})
                with open(guid_path) as f:
                    guid = f.readline().strip()
                    mac = self._get_mac_from_guid(guid)
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
            self._config_vf_mac_address_mlnx4(pf_mlx_dev, vf_pci_slot,
                                              hca_port, vguid)
        elif (vf_device_type == constants.MLNX5_DEVICE_TYPE):
            self._config_vf_mac_address_mlnx5(pf_net_dev, pf_mlx_dev, vf_idx,
                                              vf_pci_slot, vguid)
        else:
            LOG.error("Unsupported vf device type: %s ", vf_device_type)

    def _config_vf_mac_address_mlnx4(self, pf_mlx_dev, vf_pci_slot, hca_port,
                                     vguid):
        # NOTE(adrianc): In contrast to MLNX5 devices, it is not expected
        # that MLNX4 devices will support SET vf GUID via netlink, hence we
        # do not attempt to set them via netdev API (netlink impl).

        self._config_vf_pkey(
            IbUtils.INVALID_PKEY, IbUtils.DEFAULT_PKEY_IDX, pf_mlx_dev,
            vf_pci_slot, hca_port)

        guid_idx = self._get_guid_idx_mlx4(pf_mlx_dev, vf_pci_slot,
                                           hca_port)
        path = constants.MLNX4_ADMIN_GUID_PATH % (pf_mlx_dev, hca_port,
                                                  guid_idx)
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

    def _set_vf_guid_sysfs_mlnx5(self, guid, pf_mlx_dev, vf_idx):
        guid_node = constants.MLNX5_GUID_NODE_PATH % {'module': pf_mlx_dev,
                                                      'vf_num': int(vf_idx)}
        guid_port = constants.MLNX5_GUID_PORT_PATH % {'module': pf_mlx_dev,
                                                      'vf_num': int(vf_idx)}
        for path in (guid_node, guid_port):
            sys_api.sys_write(path, guid)

    def _config_vf_mac_address_mlnx5(self, pf_net_dev, pf_mlx_dev, vf_idx,
                                     vf_pci_slot, vguid):
        try:
            net_dev_api.set_vf_guid(pf_net_dev, vf_idx, vguid)
        except api_exceptions.NetlinkRuntimeError as e:
            LOG.debug("Failed to set vf guid via netdev API. "
                     "%s, attempting to set vf guid via sysfs", str(e))
            self._set_vf_guid_sysfs_mlnx5(vguid, pf_mlx_dev, vf_idx)

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
