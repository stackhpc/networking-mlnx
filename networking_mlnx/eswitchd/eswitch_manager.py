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
import six

from networking_mlnx._i18n import _
from networking_mlnx.eswitchd.common import constants
from networking_mlnx.eswitchd.common import exceptions
from networking_mlnx.eswitchd.utils import ib_utils
from networking_mlnx.eswitchd.utils import pci_utils
from networking_mlnx.internal.netdev_ops import api as net_dev_api
from networking_mlnx.internal.netdev_ops import constants as netdev_consts

LOG = logging.getLogger(__name__)


class PfPciDeviceWrapper(object):
    """A wrapper classs for PF PCI device used to perform Operations
    on device.

    """
    def __init__(self, dev_name):
        """Constructor

        :param dev_name: pf netdev name
        """
        # TODO(adrianc): these two (pci and ib utils) need to be sorted out
        # as the responsibility of each class is not really well defined.
        self.pci_utils = pci_utils.pciUtils()
        self.ib_utils = ib_utils.IbUtils()
        self.net_dev_name = dev_name
        self.mlx_dev_name = self.pci_utils.get_pf_mlx_dev(dev_name)
        # NOTE(adrianc): hca_port is required for ConnectX-3 family devices
        self.hca_port = self.pci_utils.get_eth_port(dev_name)
        self.device_type = self.pci_utils.get_pf_device_type(dev_name)

    def get_vfs_info(self):
        return self.pci_utils.get_vfs_info(self.net_dev_name)

    def is_assigned_vf(self, vf_num):
        return self.pci_utils.is_assigned_vf(self.net_dev_name, vf_num)

    def get_vfs_macs_ib(self, vf_idxs):
        return self.ib_utils.get_vfs_macs_ib(
            self.net_dev_name, self.mlx_dev_name, self.hca_port, vf_idxs,
            self.device_type)

    def get_vf_state(self, vf_index):
        # TODO(adrianc) implement
        raise NotImplementedError(_("get_vf_state() not implemented."))

    def set_vf_state(self, vf_index, state):
        """Set VF admin state
        """
        if self.device_type == constants.MLNX4_DEVICE_TYPE:
            LOG.warning(
                "Changing VF admin state for MLX4 device is not supported.")
        else:
            net_dev_api.set_vf_admin_state(self.net_dev_name, vf_index, state)

    def config_port_up(self):
        """Set PF netdev link state to UP
        """
        net_dev_api.set_link_state(self.net_dev_name,
                                   netdev_consts.LINK_STATE_UP)

    def config_vf_mac_address(self, vf_idx, vf_pci_slot, mac=None):
        """Configure VF mac address

        :param vf_idx: VF index
        :param vf_pci_slot: VF PCI address
        :param mac: mac address, None value means mac address cleanup
        """
        self.ib_utils.config_vf_mac_address(
            self.net_dev_name, self.mlx_dev_name, vf_idx, vf_pci_slot,
            self.device_type, self.hca_port, mac)

    def config_vlan_ib(self, pci_slot, vlan):
        """Set VF IB VLAN (Set Infiniband PKEY for VF, ConnectX3 Only)

        :param pci_slot: VF PCI slot address
        :param vlan: VLAN ID
        """
        return self.ib_utils.config_vlan_ib(
            self.mlx_dev_name, pci_slot, self.device_type, self.hca_port,
            vlan)


class EmbSwitch(object):
    """Class to manage logical embedded switch entity.

    Embedded Switch object is logical entity representing all VFs
    connected to a physical network
    Each physical network is mapped to PF network device interface,
    meaning all its VF.
    :ivar pci_slot_map: dictionary for mapping each pci slot to vf index
    :ivar pci_dev_wrapper: pci device wrapper
    """

    def __init__(self, dev_name):
        """Constructor

        :param dev_name: physical network device name
        """
        self.pf_dev_wrapper = PfPciDeviceWrapper(dev_name)
        self.dev_name = dev_name
        self.vf_pci_to_idx = {}
        vfs_info = self.pf_dev_wrapper.get_vfs_info()
        for pci_slot, vf_info in six.iteritems(vfs_info):
            self.vf_pci_to_idx[pci_slot] = vf_info['vf_num']

    def get_pci_slot_list(self):
        """Get list of VF addresses."""
        return self.vf_pci_to_idx.keys()

    def get_devices_info(self, assigned_only=False):
        """Get Virtual Functions mac and pci slot
        information

        :param assigned_only: Retrieve only assigned devices
        :return: list of VF pair (mac address, pci slot)
        """
        vf_to_pci_slot_mapping = {}
        assigned_devices_info = []
        for pci_slot, vf_index in six.iteritems(self.vf_pci_to_idx):
            if (assigned_only and
                    not self.pf_dev_wrapper.is_assigned_vf(vf_index)):
                continue
            vf_to_pci_slot_mapping[vf_index] = pci_slot

        if vf_to_pci_slot_mapping:
            vf_to_mac_mapping = self.pf_dev_wrapper.get_vfs_macs_ib(
                list(vf_to_pci_slot_mapping.keys()))
            for vf_index, mac in six.iteritems(vf_to_mac_mapping):
                pci_slot = vf_to_pci_slot_mapping[vf_index]
                assigned_devices_info.append((mac, pci_slot))
        return assigned_devices_info

    def get_device_admin_state(self, pci_slot):
        """Get device admin state.

        :param pci_slot: Virtual Function address
        :return: device admin state, one of: netdev_consts.ADMIN_STATE_*
        """
        vf_index = self._get_vf_index(pci_slot)
        return self.pf_dev_wrapper.get_vf_state(vf_index)

    def set_device_admin_state(self, pci_slot, admin_state):
        """Set device admin state.

        :param pci_slot: Virtual Function address
        :param admin_state: device admin state,
               one of: netdev_consts.ADMIN_STATE_*
        """
        vf_index = self._get_vf_index(pci_slot)
        return self.pf_dev_wrapper.set_vf_state(vf_index, admin_state)

    def _get_vf_index(self, pci_slot):
        vf_index = self.vf_pci_to_idx.get(pci_slot)
        if vf_index is None:
            raise exceptions.DeviceNotFoundException(
                _("Cannot find vf index for pci slot %s") % pci_slot)
        return vf_index

    def get_device_ib_mac(self, pci_slot):
        """Get IB mac address for given Virtual Function address

        :param pci_slot: VF pci address
        :return: MAC address of virtual function
        """
        vf_index = self._get_vf_index(pci_slot)
        mac = None
        if self.pf_dev_wrapper.is_assigned_vf(vf_index):
            macs = self.pf_dev_wrapper.get_vfs_macs_ib([vf_index])
            mac = macs.get(vf_index)
        return mac

    def set_device_ib_mac(self, pci_slot, mac=None):
        """Set IB mac address for given virtual function address

        :param pci_slot: VF pci address
        :param mac: MAC address for the VF, if None then the device
                    mac will be invalidated.

        Note:
        When invalidating MAC address (mac=None), VF admin state will change
        to disable.
        """
        vf_index = self._get_vf_index(pci_slot)
        self.pf_dev_wrapper.config_vf_mac_address(vf_index, pci_slot, mac)

    def config_vlan_ib(self, pci_slot, vlan):
        """Set VF IB VLAN (Set Infiniband PKEY for VF, ConnectX3 Only)

        :param pci_slot: VF PCI slot address
        :param vlan: VLAN ID
        """
        self.pf_dev_wrapper.config_vlan_ib(pci_slot, vlan)


class ESwitchManager(object):
    """Manages logical Embedded Switch entities for physical network."""

    def __new__(cls):
        # make it a singleton
        if not hasattr(cls, '_instance'):
            cls._instance = super(ESwitchManager, cls).__new__(cls)
            cls.emb_switches_map = {}
            cls.pci_slot_map = {}
        return cls._instance

    def __init__(self):
        super(ESwitchManager, self).__init__()
        self.pci_utils = pci_utils.pciUtils()

    def device_exists(self, pci_slot):
        """Verify if device exists.

        Check if VF pci slot exists
        :param pci_slot: VF address
        """
        embedded_switch = self._get_emb_eswitch(pci_slot)
        if embedded_switch:
            return True
        return False

    def get_assigned_devices_info(self, phys_nets=None):
        """Get all assigned devices.

        Get all assigned devices belongs to given embedded switch
        :param phys_net: list of physical networks, if none get assigned
                         devices from all physical networks
        :return: set of assigned VFs (mac address, pci slot) pairs
        """
        if phys_nets:
            eswitch_objects = set()
            for net in phys_nets:
                emb_sw = self.emb_switches_map.get(net)
                if emb_sw:
                    eswitch_objects.add(emb_sw)
        else:
            eswitch_objects = set()
            for eswitch_list in self.emb_switches_map.values():
                eswitch_objects |= set(eswitch_list)
        assigned_devices = set()
        for embedded_switch in eswitch_objects:
            for device in embedded_switch.get_devices_info(True):
                assigned_devices.add(device)
        return assigned_devices

    def get_device_from_mac(self, mac, phys_net=None):
        """Get PCI VF address from Mac address

        :param mac: MAC address
        :param phys_net: The physical network name the mac address resides in
        :return: VF PCI address that has `mac` assigned as its L2 address
                 or None if no matching device found.
        """
        if phys_net:
            eswitch_objects = self.emb_switches_map.get(phys_net, set())
        else:
            eswitch_objects = set()
            for eswitch_list in self.emb_switches_map.values():
                eswitch_objects |= set(eswitch_list)

        for embedded_switch in eswitch_objects:
            for mac_addr, slot in embedded_switch.get_devices_info():
                if mac == mac_addr:
                    # TODO(adrianc) decide if we want to deal with same MAC in
                    # multiple fabrics.
                    return slot
        return None

    def get_device_admin_state(self, pci_slot):
        """Get device state.

        Get the device state (up/enable, down/disable, or auto)
        :param pci_slot: VF PCI slot
        :return: device admin state, one of: netdev_consts.ADMIN_STATE_*
        """
        embedded_switch = self._get_emb_eswitch(pci_slot)
        if embedded_switch:
            return embedded_switch.get_device_admin_state(pci_slot)
        return netdev_consts.ADMIN_STATE_DOWN

    def set_device_admin_state(self, pci_slot, admin_state):
        """Set device admin state

        :param pci_slot: pci slot
        :param admin_state: device admin state,
               one of: netdev_consts.ADMIN_STATE_*
        """
        embedded_switch = self._get_emb_eswitch(pci_slot)
        if embedded_switch:
            embedded_switch.set_device_admin_state(pci_slot, admin_state)

    def set_device_ib_mac(self, pci_slot, mac=None):
        """Set IB mac address for given virtual function address

        :param pci_slot: VF pci address
        :param mac: MAC address for the VF, if None then the device
                    mac will be invalidated.

        Note:
        When setting a valid MAC address, the VF admin state will change to
        enable. When invalidating MAC address (mac=None),
        VF admin state will change to disable.
        """
        embedded_switch = self._get_emb_eswitch(pci_slot)
        if embedded_switch:
            embedded_switch.set_device_ib_mac(pci_slot, mac)

    def config_vlan_ib(self, pci_slot, vlan):
        """Set VF IB VLAN (Set Infiniband PKEY for VF)

        :param pci_slot: VF PCI address
        :param vlan: VLAN ID
        """
        embedded_switch = self._get_emb_eswitch(pci_slot)
        if embedded_switch:
            embedded_switch.config_vlan_ib(pci_slot, vlan)

    def _process_emb_switch_map(self, phys_net, dev_name):
        """Process emb_switch_map
        :param phys_net: physical network
        :param dev_name: device name
        """
        emb_switches = self.emb_switches_map.get(phys_net, [])
        for switch in emb_switches:
            if switch.dev_name == dev_name:
                # Embedded switch already created skip
                return

        # Create a new Embedded switch

        if (self.pci_utils.is_sriov_pf(dev_name) and
                self.pci_utils.verify_vendor_pf(dev_name, constants.VENDOR)):
            self._create_emb_switch(phys_net, dev_name)
        else:
            LOG.error("PF %s must have Mellanox Vendor ID, "
                      "SR-IOV and driver module enabled. "
                      "Terminating!", dev_name)
            raise SystemExit(1)

    def discover_devices(self, fabrics):
        """Discover which Virtual functions to manage.

        Discover devices, and create embedded switch object for network device
        :param fabrics: list of <physnet, pf netdev> tuples
        """
        for phys_net, dev_name in fabrics:
            self._process_emb_switch_map(phys_net, dev_name)

    def _create_emb_switch(self, phys_net, dev_name):
        embedded_switch = EmbSwitch(dev_name)
        # TODO(adrianc): eswitch service should not need to set PF netdev
        # link state. This should be handled during deployment. In the furure,
        # consider removing config_port_up.
        embedded_switch.pf_dev_wrapper.config_port_up()

        self.emb_switches_map.setdefault(phys_net, []).append(embedded_switch)
        for pci_slot in embedded_switch.get_pci_slot_list():
            self.pci_slot_map[pci_slot] = embedded_switch

    def _get_emb_eswitch(self, pci_slot):
        """Get embedded switch.

        Get embedded switch by pci slot
        :param pci_slot: pci slot
        """
        embedded_switch = self.pci_slot_map.get(pci_slot)
        return embedded_switch
