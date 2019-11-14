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

import mock

from networking_mlnx.eswitchd.common import exceptions
from networking_mlnx.eswitchd import eswitch_manager as esm
from networking_mlnx.internal.netdev_ops import constants as netdev_const
from networking_mlnx.tests import base


class TestCreateESwitchManager(base.TestCase):
    SCANNED_DEVICES = [('0000:06:00.1', 0),
                       ('0000:06:00.2', 1),
                       ('0000:06:00.3', 2)]

    @staticmethod
    def cleanup():
        if hasattr(esm.ESwitchManager, '_instance'):
            del esm.ESwitchManager._instance

    def test_create_eswitch_mgr_fail_vendor(self):
        device_mappings = [('physnet1', 'p6p1')]
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils.pciUtils."
                        "is_sriov_pf",
                        return_value=True), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils.pciUtils."
                           "verify_vendor_pf", return_value=False):
            eswitch_mgr = esm.ESwitchManager()
            self.addCleanup(self.cleanup)
            self.assertRaises(SystemExit,
                              eswitch_mgr.discover_devices,
                              device_mappings)

    def test_create_eswitch_mgr_fail_pf(self):
        device_mappings = [('physnet1', 'p6p1')]
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                        "pciUtils.is_sriov_pf", return_value=False), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                           "pciUtils.verify_vendor_pf", return_value=True):
            eswitch_mgr = esm.ESwitchManager()
            self.addCleanup(self.cleanup)
            self.assertRaises(SystemExit,
                              eswitch_mgr.discover_devices,
                              device_mappings)

    def test_create_eswitch_mgr_ok(self):
        device_mappings = [('physnet1', 'p6p1')]
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                        "pciUtils.is_sriov_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                           "pciUtils.verify_vendor_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.eswitch_manager."
                           "EmbSwitch"):
            eswitch_mgr = esm.ESwitchManager()
            self.addCleanup(self.cleanup)
            eswitch_mgr.discover_devices(device_mappings)


class TestESwitchManagerApi(base.TestCase):
    SCANNED_DEVICES = [('0000:06:00.1', 0),
                       ('0000:06:00.2', 1),
                       ('0000:06:00.3', 2)]

    VF_INFO_OUT = {'0000:06:00.1': {'vf_num': '0', 'vf_device_type': 'MLNX5'},
                   '0000:06:00.2': {'vf_num': '1', 'vf_device_type': 'MLNX5'},
                   '0000:06:00.3': {'vf_num': '2', 'vf_device_type': 'MLNX5'}}

    ASSIGNED_MAC = '00:00:00:00:00:66'
    PCI_SLOT = '0000:06:00.1'
    PCI_IDX = '0'
    WRONG_MAC = '00:00:00:00:00:67'
    WRONG_PCI = "0000:06:00.6"

    def setUp(self):
        super(TestESwitchManagerApi, self).setUp()
        device_mappings = [('physnet1', 'p6p1')]
        self.eswitch_mgr = esm.ESwitchManager()
        self.addCleanup(self.cleanup)
        self._set_eswitch_manager(self.eswitch_mgr, device_mappings)

    @staticmethod
    def cleanup():
        if hasattr(esm.ESwitchManager, '_instance'):
            del esm.ESwitchManager._instance

    def _set_eswitch_manager(self, eswitch_mgr, device_mappings):
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                        "pciUtils.is_sriov_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils.pciUtils."
                           "verify_vendor_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.eswitch_manager."
                "PfPciDeviceWrapper") as mock_dev_wrapper_cls:
            self.mock_dev_wrapper = mock.Mock()
            mock_dev_wrapper_cls.return_value = self.mock_dev_wrapper
            self.mock_dev_wrapper.get_vfs_info.return_value = self.VF_INFO_OUT
            eswitch_mgr.discover_devices(device_mappings)

    def test_discover_devices_with_device(self):
        device_mappings = [('physnet1', 'p6p1'), ('physnet1', 'p6p2')]
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                        "pciUtils.is_sriov_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils.pciUtils."
                           "verify_vendor_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.eswitch_manager."
                           "ESwitchManager._create_emb_switch") as emb_switch:
            self.eswitch_mgr.discover_devices(device_mappings)
            self.assertTrue(emb_switch.called)

    def test_get_assigned_devices_info(self):
        with mock.patch("networking_mlnx.eswitchd.eswitch_manager.EmbSwitch."
                        "get_devices_info",
                        return_value=[(self.ASSIGNED_MAC, self.PCI_SLOT)]):
            result = self.eswitch_mgr.get_assigned_devices_info()
            self.assertIn(self.ASSIGNED_MAC, list(result)[0])
            self.assertIn(self.PCI_SLOT, list(result)[0])

    def test_get_assigned_devices_info_multiple_nics_for_physnet(self):
        device_mappings = [('physnet1', 'p6p1'), ('physnet1', 'p6p2')]
        devices_info = {
            'p6p1': [(self.ASSIGNED_MAC, self.PCI_SLOT)],
            'p6p2': [(self.WRONG_MAC, self.WRONG_PCI)],
        }

        def my_get_devices_info(self, assigned_only):
            return devices_info[self.dev_name]

        self._set_eswitch_manager(self.eswitch_mgr, device_mappings)

        with mock.patch("networking_mlnx.eswitchd.eswitch_manager.EmbSwitch."
                        "get_devices_info",
                        side_effect=my_get_devices_info,
                        autospec=True):
            result = self.eswitch_mgr.get_assigned_devices_info()
            self.assertIn(devices_info['p6p1'][0], list(result))
            self.assertIn(devices_info['p6p2'][0], list(result))

    def test_get_device_admin_state_up(self):
        self.mock_dev_wrapper.get_vf_state.return_value = \
            netdev_const.ADMIN_STATE_UP
        result = self.eswitch_mgr.get_device_admin_state(self.PCI_SLOT)
        self.assertEqual(netdev_const.ADMIN_STATE_UP, result)

    def test_get_device_admin_state_down(self):
        self.mock_dev_wrapper.get_vf_state.return_value = \
            netdev_const.ADMIN_STATE_DOWN
        result = self.eswitch_mgr.get_device_admin_state(self.PCI_SLOT)
        self.assertEqual(netdev_const.ADMIN_STATE_DOWN, result)

    def test_get_device_admin_state_auto(self):
        self.mock_dev_wrapper.get_vf_state.return_value = \
            netdev_const.ADMIN_STATE_AUTO
        result = self.eswitch_mgr.get_device_admin_state(self.PCI_SLOT)
        self.assertEqual(netdev_const.ADMIN_STATE_AUTO, result)

    def test_set_device_admin_state(self):
        self.eswitch_mgr.set_device_admin_state(self.PCI_SLOT,
                                                netdev_const.ADMIN_STATE_UP)
        self.mock_dev_wrapper.set_vf_state.assert_called_with(
            self.PCI_IDX, netdev_const.ADMIN_STATE_UP)

    def _mock_device_exists(self, pci_slot, mac_address, expected_result):
        with mock.patch("neutron.plugins.ml2.drivers.mech_sriov.agent."
                        "eswitch_manager.EmbSwitch.get_pci_device",
                        return_value=self.ASSIGNED_MAC):
            result = self.eswitch_mgr.device_exists(mac_address,
                                                    pci_slot)
            self.assertEqual(expected_result, result)

    def test_device_exists_true(self):
        self.assertTrue(self.eswitch_mgr.device_exists(self.PCI_SLOT))

    def test_device_exists_false(self):
        self.assertFalse(self.eswitch_mgr.device_exists(self.WRONG_PCI))

    def test_process_emb_switch(self):
        device_mappings = [('physnet1', 'p6p1'), ('physnet1', 'p6p2')]
        phys_net = 'physnet1'
        dev_name = 'p6p1'
        self._set_eswitch_manager(self.eswitch_mgr, device_mappings)
        with mock.patch("networking_mlnx.eswitchd.utils.pci_utils."
                        "pciUtils.is_sriov_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.utils.pci_utils.pciUtils."
                           "verify_vendor_pf", return_value=True), \
                mock.patch("networking_mlnx.eswitchd.eswitch_manager."
                           "ESwitchManager._create_emb_switch") as emb_switch:
            self.eswitch_mgr._process_emb_switch_map(phys_net,
                                                     dev_name)
            self.assertFalse(emb_switch.called)

    def test_create_emb_switch(self):
        DEVICES = [('0000:04:00.1', 0),
                   ('0000:04:00.2', 1)]
        with mock.patch("networking_mlnx.eswitchd."
                        "eswitch_manager.EmbSwitch") as mock_esw:
            esw_inst = mock.Mock()
            mock_esw.return_value = esw_inst
            esw_inst.get_pci_slot_list.return_value = [
                slot for slot, idx in DEVICES]
            physnet = 'test_create_emb_switch'
            self.assertNotIn(physnet, self.eswitch_mgr.emb_switches_map)
            # create a new embedded switch
            self.eswitch_mgr._create_emb_switch(physnet, 'dev1')
            self.assertIn(physnet, self.eswitch_mgr.emb_switches_map)
            for slot, _ in DEVICES:
                self.assertIn(slot, self.eswitch_mgr.pci_slot_map)

    def test_create_emb_switch_zero_vfs(self):
        with mock.patch("networking_mlnx.eswitchd."
                        "eswitch_manager.EmbSwitch") as mock_esw:
            esw_inst = mock.Mock()
            mock_esw.return_value = esw_inst
            esw_inst.get_pci_slot_list.return_value = []
            physnet = 'test_create_emb_switch'
            self.eswitch_mgr.emb_switches_map = {}
            self.eswitch_mgr.pci_slot_map = {}
            # create a new embedded switch
            self.eswitch_mgr._create_emb_switch(physnet, 'dev1')
            self.assertIn(physnet, self.eswitch_mgr.emb_switches_map)
            self.assertFalse(self.eswitch_mgr.pci_slot_map)


class TestEmbSwitch(base.TestCase):
    DEV_NAME = "eth2"
    PHYS_NET = "default"
    ASSIGNED_MAC = '00:00:00:00:00:66'
    PCI_SLOT = "0000:06:00.1"
    WRONG_PCI_SLOT = "0000:06:00.4"
    VF_TO_MAC_MAPPING = {'0': '00:00:00:00:00:11',
                         '1': '00:00:00:00:00:22',
                         '2': '00:00:00:00:00:33'}
    VF_INFO_OUT = {'0000:06:00.1': {'vf_num': '0', 'vf_device_type': 'MLNX5'},
                   '0000:06:00.2': {'vf_num': '1', 'vf_device_type': 'MLNX5'},
                   '0000:06:00.3': {'vf_num': '2', 'vf_device_type': 'MLNX5'}}
    EXPECTED_DEVICE_INFO = [('00:00:00:00:00:11', '0000:06:00.1'),
                            ('00:00:00:00:00:22', '0000:06:00.2'),
                            ('00:00:00:00:00:33', '0000:06:00.3')]

    def setUp(self):
        super(TestEmbSwitch, self).setUp()
        with mock.patch("networking_mlnx.eswitchd.eswitch_manager."
                        "PfPciDeviceWrapper") as mock_dev_wrapper_cls:
            self.mock_dev_wrapper = mock.Mock()
            mock_dev_wrapper_cls.return_value = self.mock_dev_wrapper
            self.mock_dev_wrapper.get_vfs_info.return_value = self.VF_INFO_OUT
            self.emb_switch = esm.EmbSwitch(self.DEV_NAME)

    def test_get_devices_info_assigned_only_true(self):
        def my_is_assigned_vf(vf_idx):
            return vf_idx != 0
        del self.VF_TO_MAC_MAPPING['0']
        self.mock_dev_wrapper.get_vfs_macs_ib.return_value = \
            self.VF_TO_MAC_MAPPING
        self.mock_dev_wrapper.is_assigned_vf.side_effect = my_is_assigned_vf

        result = self.emb_switch.get_devices_info(True)
        self.assertNotIn(('00:00:00:00:00:11', '0000:06:00.1'), result)
        self.assertIn(('00:00:00:00:00:22', '0000:06:00.2'), result)
        self.assertIn(('00:00:00:00:00:33', '0000:06:00.3'), result)
        self.assertEqual(2, len(result))

    def test_get_devices_info_assigned_only_false(self):
        self.mock_dev_wrapper.get_vfs_macs_ib.return_value = \
            self.VF_TO_MAC_MAPPING
        self.mock_dev_wrapper.is_assigned_vf.return_value = False
        result = self.emb_switch.get_devices_info()
        self.assertEqual(set(self.EXPECTED_DEVICE_INFO), set(result))

    def test_get_device_state_ok(self):
        self.mock_dev_wrapper.get_vf_state.return_value = \
            netdev_const.ADMIN_STATE_DOWN
        result = self.emb_switch.get_device_admin_state(self.PCI_SLOT)
        self.assertEqual(netdev_const.ADMIN_STATE_DOWN, result)

    def test_get_device_state_fail(self):
        self.mock_dev_wrapper.get_vf_state.return_value = \
            netdev_const.ADMIN_STATE_DOWN
        self.assertRaises(exceptions.DeviceNotFoundException,
                          self.emb_switch.get_device_admin_state,
                          self.WRONG_PCI_SLOT)

    def test_set_device_admin_state_ok(self):
        self.emb_switch.set_device_admin_state(
            self.PCI_SLOT, netdev_const.ADMIN_STATE_UP)
        self.assertTrue(self.mock_dev_wrapper.set_vf_state.called)

    def test_set_device_admin_state_fail(self):
        self.assertRaises(exceptions.DeviceNotFoundException,
                          self.emb_switch.set_device_admin_state,
                          self.WRONG_PCI_SLOT, netdev_const.ADMIN_STATE_UP)
        self.assertFalse(self.mock_dev_wrapper.set_vf_state.called)

    def test_get_device_ib_mac(self):
        self.mock_dev_wrapper.is_assigned_vf.return_value = True
        self.mock_dev_wrapper.get_vfs_macs_ib.return_value = {
            '0': self.ASSIGNED_MAC}
        result = self.emb_switch.get_device_ib_mac(self.PCI_SLOT)
        self.assertEqual(self.ASSIGNED_MAC, result)

    def test_get_device_ib_mac_unassigned(self):
        self.mock_dev_wrapper.is_assigned_vf.return_value = False
        result = self.emb_switch.get_device_ib_mac(self.PCI_SLOT)
        self.assertIsNone(result)

    def test_get_pci_list(self):
        result = self.emb_switch.get_pci_slot_list()
        self.assertEqual(set(self.VF_INFO_OUT.keys()), set(result))

    def test_set_device_ib_mac(self):
        self.emb_switch.set_device_ib_mac(self.PCI_SLOT, self.ASSIGNED_MAC)
        self.assertTrue(self.mock_dev_wrapper.config_vf_mac_address.called)

    def test_set_device_ib_mac_invalid_device(self):
        self.assertRaises(exceptions.DeviceNotFoundException,
                          self.emb_switch.set_device_ib_mac,
                          self.WRONG_PCI_SLOT,
                          self.ASSIGNED_MAC)
        self.assertFalse(self.mock_dev_wrapper.config_vf_mac_address.called)

    def test_config_vlan_ib(self):
        self.emb_switch.config_vlan_ib(self.PCI_SLOT, 12)
        self.assertTrue(self.mock_dev_wrapper.config_vlan_ib.called)


class TestPfPciDeviceWrapper(base.TestCase):
    # TODO(adrianc): add Tests
    pass
