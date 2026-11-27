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

import unittest.mock as mock

from networking_mlnx.eswitchd.common import constants
from networking_mlnx.eswitchd.common import exceptions
from networking_mlnx.eswitchd.utils import ib_utils
from networking_mlnx.internal.netdev_ops import exceptions as api_exceptions
from networking_mlnx.tests import base


class TestIbUtils(base.TestCase):

    def setUp(self):
        super(TestIbUtils, self).setUp()
        self.ib_utils = ib_utils.IbUtils()

    def test__get_mac_from_guid_mlx4(self):
        guid = "1122334455667788"
        expected_mac = "11:22:33:66:77:88"
        mac = self.ib_utils._get_mac_from_guid(guid)
        self.assertEqual(expected_mac, mac)

    def test__get_mac_from_guid_mlx5(self):
        guid = "11:22:33:44:55:66:77:88"
        expected_mac = "11:22:33:66:77:88"
        mac = self.ib_utils._get_mac_from_guid(guid)
        self.assertEqual(expected_mac, mac)

    def test__get_mac_from_guid_invalid_guid(self):
        self.assertRaises(exceptions.InvalidGUIDFormatException,
                          self.ib_utils._get_mac_from_guid, "112233445566")
        self.assertRaises(exceptions.InvalidGUIDFormatException,
                          self.ib_utils._get_mac_from_guid,
                          "11:22:33:44:55:66")
        self.assertRaises(exceptions.InvalidGUIDFormatException,
                          self.ib_utils._get_mac_from_guid, "rubbish")

    @mock.patch.object(ib_utils.net_dev_api, "get_vf_guid")
    def test__get_vfs_ib_mac_netlink(self, get_vf_guid_mock):
        def mock_call(pf_ifname, vf_idx):
            guids = ["11:22:33:44:55:66:77:88", "aa:bb:cc:dd:ee:ff:00:11"]
            return guids[vf_idx]
        get_vf_guid_mock.side_effect = mock_call

        mac_map = self.ib_utils._get_vfs_ib_mac_netdev_api("ib0", [0, 1])
        expected_mac_map = {0: "11:22:33:66:77:88",
                            1: "aa:bb:cc:ff:00:11"}
        self.assertDictEqual(expected_mac_map, mac_map)

    @mock.patch.object(ib_utils.net_dev_api, "get_vf_guid",
                       side_effect=api_exceptions.NetlinkAttrNotFoundError(""))
    def test__get_vfs_ib_mac_netlink_fail(self, get_vf_guid_mock):
        mac_map = self.ib_utils._get_vfs_ib_mac_netdev_api("ib0", [0, 1])
        self.assertIsNone(mac_map)

    def test_guid_round_trip(self):
        # eswitchd reads back the mac address of the GUID it set.
        mac = 'fa:16:3e:a6:71:17'
        for device_type in (constants.MLNX4_DEVICE_TYPE,
                            constants.MLNX5_DEVICE_TYPE):
            guid = self.ib_utils._get_guid_from_mac(mac, device_type)
            self.assertEqual(mac, self.ib_utils._get_mac_from_guid(guid))

    @mock.patch.object(ib_utils.glob, 'glob')
    @mock.patch.object(ib_utils.IbUtils, '_get_gid_to_vf_idx_mapping')
    def test_get_vfs_macs_ib_mlnx4(self, gid_mapping, glob_mock):
        # ConnectX-3: the GUIDs are read from the admin GUIDs of the port,
        # whether or not the VF is assigned.
        gid_mapping.return_value = {5: 3}
        path = constants.MLNX4_ADMIN_GUID_PATH % ('mlx4_0', 1, '%s')
        glob_mock.return_value = [path % 4, path % 5]
        with mock.patch('builtins.open', mock.mock_open(
                read_data='fa163e0000a67117\n')) as open_mock:
            macs = self.ib_utils.get_vfs_macs_ib(
                'ib0', 'mlx4_0', 1, [3], constants.MLNX4_DEVICE_TYPE)
        self.assertEqual({3: 'fa:16:3e:a6:71:17'}, macs)
        open_mock.assert_called_once_with(path % 5)
        glob_mock.assert_called_once_with(path % '[1-9]*')

    @mock.patch.object(ib_utils.glob, 'glob')
    @mock.patch.object(ib_utils.IbUtils, '_get_gid_to_vf_idx_mapping')
    def test_get_vfs_macs_ib_mlnx4_invalid_guid(self, gid_mapping,
                                                glob_mock):
        gid_mapping.return_value = {5: 3}
        glob_mock.return_value = [
            constants.MLNX4_ADMIN_GUID_PATH % ('mlx4_0', 1, 5)]
        with mock.patch('builtins.open', mock.mock_open(
                read_data=constants.MLNX4_INVALID_GUID + '\n')):
            macs = self.ib_utils.get_vfs_macs_ib(
                'ib0', 'mlx4_0', 1, [3], constants.MLNX4_DEVICE_TYPE)
        self.assertEqual({3: constants.INVALID_MAC}, macs)

    @mock.patch.object(ib_utils.net_dev_api, "get_vf_guid",
                       return_value="fa:16:3e:00:00:a6:71:17")
    def test_get_vfs_macs_ib_mlnx5(self, get_vf_guid_mock):
        macs = self.ib_utils.get_vfs_macs_ib(
            'ib0', 'mlx5_0', None, [3], constants.MLNX5_DEVICE_TYPE)
        self.assertEqual({3: 'fa:16:3e:a6:71:17'}, macs)
        get_vf_guid_mock.assert_called_once_with('ib0', 3)

    @mock.patch.object(ib_utils.net_dev_api, "get_vf_guid",
                       side_effect=api_exceptions.NetlinkAttrNotFoundError(""))
    def test_get_vfs_macs_ib_mlnx5_sysfs(self, get_vf_guid_mock):
        with mock.patch('builtins.open', mock.mock_open(
                read_data='fa:16:3e:00:00:a6:71:17\n')) as open_mock:
            macs = self.ib_utils.get_vfs_macs_ib(
                'ib0', 'mlx5_0', None, [3], constants.MLNX5_DEVICE_TYPE)
        self.assertEqual({3: 'fa:16:3e:a6:71:17'}, macs)
        open_mock.assert_called_once_with(
            constants.MLNX5_GUID_NODE_PATH % {'module': 'mlx5_0',
                                              'vf_num': 3})

    @mock.patch.object(ib_utils.IbUtils, '_config_vf_pkey')
    @mock.patch.object(ib_utils.IbUtils, '_get_pkey_idx', return_value=3)
    def test__config_vlan_ib_mlnx4(self, get_pkey_idx, config_vf_pkey):
        self.ib_utils._config_vlan_ib_mlnx4('mlx4_0', '0000:06:00.2', 1, 5)
        get_pkey_idx.assert_called_once_with(5, 'mlx4_0', 1)
        config_vf_pkey.assert_called_once_with(
            3, ib_utils.IbUtils.DEFAULT_PKEY_IDX, 'mlx4_0', '0000:06:00.2', 1)

    @mock.patch.object(ib_utils.IbUtils, '_config_vf_pkey')
    @mock.patch.object(ib_utils.IbUtils, '_get_pkey_idx', return_value=3)
    def test__config_vlan_ib_mlnx4_untagged(self, get_pkey_idx,
                                           config_vf_pkey):
        self.ib_utils._config_vlan_ib_mlnx4('mlx4_0', '0000:06:00.2', 1, 0)
        get_pkey_idx.assert_called_once_with(
            ib_utils.IbUtils.DEFAULT_PKEY, 'mlx4_0', 1)

    @mock.patch.object(ib_utils.IbUtils, '_config_vf_pkey')
    @mock.patch.object(ib_utils.IbUtils, '_get_pkey_idx', return_value=None)
    def test__config_vlan_ib_mlnx4_missing_pkey(self, get_pkey_idx,
                                               config_vf_pkey):
        self.assertRaises(RuntimeError, self.ib_utils._config_vlan_ib_mlnx4,
                          'mlx4_0', '0000:06:00.2', 1, 5)
        config_vf_pkey.assert_called_once_with(
            ib_utils.IbUtils.INVALID_PKEY, ib_utils.IbUtils.DEFAULT_PKEY_IDX,
            'mlx4_0', '0000:06:00.2', 1)

    @mock.patch.object(ib_utils.IbUtils, '_config_vf_pkey')
    @mock.patch.object(ib_utils.IbUtils, '_get_pkey_idx')
    def test__config_vlan_ib_mlnx4_release(self, get_pkey_idx,
                                          config_vf_pkey):
        self.ib_utils._config_vlan_ib_mlnx4(
            'mlx4_0', '0000:06:00.2', 1, ib_utils.constants.UNTAGGED_VLAN_ID)
        get_pkey_idx.assert_not_called()
        config_vf_pkey.assert_called_once_with(
            ib_utils.IbUtils.INVALID_PKEY, ib_utils.IbUtils.DEFAULT_PKEY_IDX,
            'mlx4_0', '0000:06:00.2', 1)
