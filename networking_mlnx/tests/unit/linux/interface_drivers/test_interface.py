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
from neutron.agent.linux import interface as n_interface
from neutron.agent.linux import ip_lib as n_ip_lib
from oslo_config import cfg
from oslo_utils.fixture import uuidsentinel as uuids

from networking_mlnx.linux.interface_drivers import constants
from networking_mlnx.linux.interface_drivers import interface
from networking_mlnx.linux import ip_lib
from networking_mlnx.tests import base

network_db = {
    uuids.vlan_net: {'id': uuids.vlan_net,
                     constants.SEGMENTATION_ID: "14"},
    uuids.flat_net: {'id': uuids.flat_net,
                     constants.SEGMENTATION_ID: None}
}


class TestIPoIBInterfaceDriver(base.TestCase):
    def _get_networks_cb(self, filters=None, fields=None):
        # Assert calls to callback with single filter and single field
        self.assertEqual(['id'], list(filters.keys()))
        self.assertEqual(1, len(filters["id"]))
        self.assertEqual([constants.SEGMENTATION_ID], fields)
        net_id = filters["id"][0]
        return [network_db[net_id]]

    def setUp(self):
        super(TestIPoIBInterfaceDriver, self).setUp()
        self.root_dev = "ib0"
        cfg.CONF.set_override('ipoib_physical_interface', self.root_dev)
        with mock.patch.object(
                n_interface.LinuxInterfaceDriver, "__init__") as init_mock, \
                mock.patch.object(n_ip_lib, "device_exists"):
            # TODO(adrianc): Bump test-requirements neutron version to Train
            # and remove the __init__ mock above.
            def custom_init(*args, **kwargs):
                pass
            init_mock.side_effect = custom_init
            self.driver = interface.IPoIBInterfaceDriver(
                cfg.CONF, get_networks_callback=self._get_networks_cb)

    def _mock_ipoib_wrapper(self):
        patcher = mock.patch.object(ip_lib, "IPoIBWrapper")
        ipoib_cls_mock = patcher.start()
        ip_mock_inst = mock.MagicMock()
        ipoib_cls_mock.return_value = ip_mock_inst
        self.addCleanup(patcher.stop)
        return ip_mock_inst

    def _test_plug_new(self, net_id, dev_name):
        ip_mock = self._mock_ipoib_wrapper()
        ip_dev_mock = mock.Mock()
        ip_mock.add_ipoib.return_value = ip_dev_mock
        return
        self.driver.plug_new(net_id, uuids.port_id, dev_name,
                            None)
        ip_mock.add_ipoib.assert_called_with(
            dev_name, self.root_dev,
            int(network_db[uuids.uuids.vlan_net][constants.SEGMENTATION_ID]))
        ip_dev_mock.link.set_up.asset_called_once()

    def test_plug_new_vlan_network(self):
        self._test_plug_new(uuids.vlan_net, "my-ipoib-netdev")

    def test_plug_new_flat_network(self):
        self._test_plug_new(uuids.flat_net, "my-ipoib-netdev")

    @mock.patch("networking_mlnx.linux.interface_drivers.interface.LOG")
    def test_plug_new_ip_lib_raises(self, log_mock):
        ip_mock = self._mock_ipoib_wrapper()
        ip_mock.add_ipoib.side_effect = RuntimeError("Unexpected!")
        self.driver.plug_new(
            uuids.vlan_net, uuids.port_a, "my-ipoib-netdev", None)
        log_mock.error.assert_called_once()

    def test_unplug(self):
        ip_mock = self._mock_ipoib_wrapper()
        self.driver.unplug("my-ipoib-netdev", None, namespace="test-ns")
        ip_mock.del_ipoib.assert_called_with("my-ipoib-netdev")

    @mock.patch("networking_mlnx.linux.interface_drivers.interface.LOG")
    def test_unplug_ip_lib_raises(self, log_mock):
        ip_mock = self._mock_ipoib_wrapper()
        ip_mock.del_ipoib.side_effect = RuntimeError("Unexpected!")
        self.driver.unplug("my-ipoib-netdev")
        log_mock.error.assert_called_once()
