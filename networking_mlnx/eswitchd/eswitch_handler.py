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

from oslo_log import log as logging

from networking_mlnx.eswitchd.common import constants
from networking_mlnx.eswitchd import eswitch_manager
from networking_mlnx.eswitchd.utils import pci_utils
from networking_mlnx.internal.netdev_ops import constants as netdev_consts


LOG = logging.getLogger(__name__)


class eSwitchHandler(object):

    def __init__(self, fabrics=None):
        """Constructor

        :param fabrics: list of <physnet, pf netdev> tuples
        """
        self.eswitch_mgr = eswitch_manager.ESwitchManager()
        self.pci_utils = pci_utils.pciUtils()
        self.devices = set()
        if fabrics:
            self.add_fabrics(fabrics)

    def add_fabrics(self, fabrics):
        """Add fabrics to ESwitchManager

        :param fabrics: list of <physnet, pf netdev> tuples
        """
        self.eswitch_mgr.discover_devices(fabrics)

    def get_vnics(self, fabrics):
        """Get assigned vnics

        :param fabrics: list of physnets to get attached vnics from or None
                        to get vnics from all physnets.
        :return: dict, keyed by the PCI slot of the VNIC
        """
        vnics = {}
        dev_infos = self.eswitch_mgr.get_assigned_devices_info(fabrics)
        for dev_info in dev_infos:
            mac, slot = dev_info
            vnics[slot] = {"mac": mac, "pci_slot": slot}
        LOG.info("vnics are %s", vnics)
        return vnics

    def plug_nic(self, fabric, device_id, vnic_mac, pci_slot):
        # TODO(adrianc): fabric and device_id are no longer used, we should
        # clean them up.
        # Keep the link down until neutron-mlnx-agent finds that the port
        # may use the network, which needs its partition key to be set up.
        # Setting the GUID may enable the link, so disable it after that too.
        # This holds even when the VF already has the GUID, for example when
        # nova-compute plugs a running instance's VF again as it starts,
        # since the same MAC address can belong to a port on another network:
        # the agent then enables the link again within a polling interval.
        self._disable_link(pci_slot)
        self.eswitch_mgr.set_device_ib_mac(pci_slot, vnic_mac)
        self._disable_link(pci_slot)
        return pci_slot

    def _disable_link(self, pci_slot):
        self.eswitch_mgr.set_device_admin_state(
            pci_slot, netdev_consts.ADMIN_STATE_DOWN)

    def delete_port(self, fabric, vnic_mac):
        pci_slot = self.eswitch_mgr.get_device_from_mac(vnic_mac, fabric)
        if pci_slot is None:
            LOG.warning("MAC address %s not found in eSwitch for Fabric: %s",
                        vnic_mac, fabric)
        else:
            self.eswitch_mgr.set_device_ib_mac(pci_slot)
        return pci_slot

    def port_release(self, fabric, pci_slot):
        """Cleanup port from any additional configurations
        (excluding mac, admin state)

        :param fabric: physnet
        :param pci_slot: VF PCI address
        :return: None - no cleanup performed.
                 True - cleanup was successful.
                 False - cleanup failed.
        """
        # cleanup Pkey(vlan) (Relevant to ConnectX3 only)
        ret = self.set_vlan(
            fabric, pci_slot, constants.UNTAGGED_VLAN_ID)
        # NOTE(adrianc) port_down() does basically nothing.
        # TODO(adrianc): revisite if we want to remove it.
        # self.port_down(fabric, pci_slot)
        return ret

    def port_up(self, fabric, pci_slot, mac=None):
        """Enable the link of a VF

        :param mac: if set, enable the link only if the VF's GUID is the
                    one derived from this mac address, so that a VF left with
                    another port's GUID can't use the network.
        """
        # TODO(adrianc): remove un-needed fabric arg
        if mac:
            current_mac = self.eswitch_mgr.get_device_ib_mac(pci_slot)
            if not current_mac or current_mac.lower() != mac.lower():
                raise RuntimeError(
                    "VF %s has the GUID of mac %s, not %s" %
                    (pci_slot, current_mac, mac))
        self.eswitch_mgr.set_device_admin_state(
            pci_slot, netdev_consts.ADMIN_STATE_UP)

    def port_down(self, fabric, pci_slot):
        # TODO(adrianc): remove un-needed fabric arg
        self.eswitch_mgr.set_device_admin_state(
            pci_slot, netdev_consts.ADMIN_STATE_DOWN)

    def set_vlan(self, fabric, pci_slot, vlan):
        if pci_slot:
            try:
                self.eswitch_mgr.config_vlan_ib(pci_slot, vlan)
                return True
            except RuntimeError as e:
                LOG.error('Set VLAN operation failed. %s', str(e))
        else:
            LOG.debug('No PCI device provided.')
        return False

    def get_eswitch_tables(self, fabrics):
        tables = {}
        # TODO(adrianc): This functionality will be removed in the future,
        # for now just retrieve a table with assigned devices info.
        for fabric in fabrics:
            # Build table for fabric.
            dev_infos = self.eswitch_mgr.get_assigned_devices_info([fabric])
            table = [['MAC', 'PCI']]
            for dev_info in dev_infos:
                mac, slot = dev_info
                table.append([mac, slot])
            tables[fabric] = table
        return tables
