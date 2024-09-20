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
from neutron_lib.utils import runtime
from oslo_config import cfg
from oslo_service import loopingcall
from oslo_utils.fixture import uuidsentinel as uuids
import six

from networking_mlnx.linux.interface_drivers import constants
from networking_mlnx.linux.interface_drivers import interface
from networking_mlnx.linux.interface_drivers import network_cache
from networking_mlnx.linux import ip_lib
from networking_mlnx.tests import base

network_db = {
    uuids.vlan_net: {'id': uuids.vlan_net,
                     constants.SEGMENTATION_ID: "14",
                     constants.PHYSICAL_NETWORK: "datacenter"},
    uuids.flat_net: {'id': uuids.flat_net,
                     constants.SEGMENTATION_ID: None,
                     constants.PHYSICAL_NETWORK: "ib-physnet"},
    uuids.vxlan_net: {'id': uuids.vxlan_net,
                      constants.SEGMENTATION_ID: 900,
                      constants.PHYSICAL_NETWORK: None}
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


class TestMultiInterfaceDriver(base.TestCase):
    def _get_networks_cb(self, filters=None, fields=None):
        if 'id' in filters.keys():
            net_id = filters["id"][0]
            return [network_db[net_id]]
        return [list(network_db.values())]

    def setUp(self):
        super(TestMultiInterfaceDriver, self).setUp()
        interface.MultiInterfaceDriver.network_cache = None
        interface.MultiInterfaceDriver._cache_init_lock = mock.MagicMock()
        self.fields = ['id', constants.PHYSICAL_NETWORK,
                       constants.SEGMENTATION_ID]
        # TODO(adrianc): Bump test-requirements neutron version to Train
        # and remove the __init__ mock above.
        self.init_patcher = mock.patch.object(
            n_interface.LinuxInterfaceDriver, "__init__")
        self.init_patcher.start()

    def tearDown(self):
        super(TestMultiInterfaceDriver, self).tearDown()
        self.init_patcher.stop()

    def _get_mock_drivers(self):
        drivers = {'datacenter': mock.Mock(),
                   'ib-physnet': mock.Mock(),
                   'nil': mock.Mock()}
        drivers['datacenter']._interface_kind_ = 'openvswitch'
        drivers['ib-physnet']._interface_kind_ = 'ipoib'
        drivers['nil']._interface_kind_ = 'openvswitch'
        return drivers

    def _init_network_cache(self):
        get_networks_mock = mock.Mock()
        get_networks_mock.return_value = {"IfYouSeeThis": "ThenYouBrokeIt"}
        fields = ['id', constants.PHYSICAL_NETWORK, constants.SEGMENTATION_ID]
        interface.MultiInterfaceDriver.network_cache = (
            network_cache.NetworkCache(get_networks_mock, fields))
        for net in six.itervalues(network_db):
            interface.MultiInterfaceDriver.network_cache.put(net['id'], net)

    @mock.patch.object(loopingcall, "FixedIntervalLoopingCall")
    @mock.patch.object(network_cache, "SafeNetworkCache")
    def test__init_network_cache(self, net_cache_cls_mock, looping_mock):
        conf = mock.Mock()
        conf.enable_multi_interface_driver_cache_maintenance = False
        interface.MultiInterfaceDriver._init_network_cache(
            conf, self._get_networks_cb, self.fields)
        net_cache_cls_mock.assert_called_once_with(self._get_networks_cb,
                                                   self.fields)
        self.assertFalse(looping_mock.called)
        # Make sure consecutive calls dont re-initialize network cache
        net_cache_cls_mock.reset_mock()
        interface.MultiInterfaceDriver._init_network_cache(
            conf, self._get_networks_cb, self.fields)
        self.assertFalse(net_cache_cls_mock.called)

    @mock.patch.object(loopingcall, "FixedIntervalLoopingCall")
    @mock.patch.object(network_cache, "SafeNetworkCache")
    def test__init_network_cache_with_cache_maintenance(self,
                                                        net_cache_cls_mock,
                                                        looping_mock):
        conf = mock.Mock()
        conf.enable_multi_interface_driver_cache_maintenance = True
        net_cache_mock = mock.Mock()
        net_cache_cls_mock.return_value = net_cache_mock
        loop_obj = mock.Mock()
        looping_mock.return_value = loop_obj
        interface.MultiInterfaceDriver._init_network_cache(
            conf, self._get_networks_cb, self.fields)
        looping_mock.assert_called_once_with(
            net_cache_mock.remove_stale_networks)
        loop_obj.start.assert_called()
        # Make sure consecutive calls dont re-spawn cleanup thread
        looping_mock.reset_mock()
        loop_obj.start.reset_mock()
        looping_mock.return_value = loop_obj
        interface.MultiInterfaceDriver._init_network_cache(
            conf, self._get_networks_cb, self.fields)
        looping_mock.assert_not_called()
        loop_obj.start.assert_not_called()

    @mock.patch.object(interface.MultiInterfaceDriver, "_process_driver_obj")
    @mock.patch.object(runtime, 'load_class_by_alias_or_classname')
    def test_load_interface_driver_mappings(self, load_cls_mock, process_mock):
        loaded_intf_drivers = []

        def load_cls(namespace, name):
            loaded_intf_drivers.append(name)
            return mock.Mock()

        load_cls_mock.side_effect = load_cls
        conf = mock.Mock()
        conf.multi_interface_driver_mappings = (
            "physnetA:openvswitch,physnetB:ipoib")
        mapping = (
            interface.MultiInterfaceDriver.load_interface_driver_mappings(
                conf, get_networks_callback=mock.Mock()))
        self.assertEqual(set(["physnetA", "physnetB"]), set(mapping.keys()))
        self.assertEqual(set(["openvswitch", "ipoib"]),
                         set(loaded_intf_drivers))

    def _check_drivers(self, first_driver_spec, second_driver_spec,
                       should_raise=False):
        type, kind = first_driver_spec
        first_mock = mock.Mock(spec=type)
        first_mock._interface_kind_ = kind
        type, kind = second_driver_spec
        second_mock = mock.Mock(spec=type)
        second_mock._interface_kind_ = kind
        mapping = {"physnetA": first_mock, "physnetB": second_mock}
        if should_raise:
            self.assertRaises(SystemExit,
                              interface.MultiInterfaceDriver._check_drivers,
                              mapping)
        else:
            interface.MultiInterfaceDriver._check_drivers(mapping)

    def test__check_drivers_different_type(self):
        self._check_drivers((n_interface.OVSInterfaceDriver, 'openvswitch'),
                       (interface.IPoIBInterfaceDriver, 'ipoib'),
                       False)

    def test__check_drivers_same_type(self):
        self._check_drivers((n_interface.OVSInterfaceDriver, 'openvswitch'),
                       (n_interface.OVSInterfaceDriver, 'openvswitch'),
                       False)

    def test__check_drivers_raises(self):
        self._check_drivers((n_interface.BridgeInterfaceDriver, 'veth'),
                            (n_interface.OVSInterfaceDriver, 'veth'),
                            True)

    def _check_process_driver_obj_ovs(self, conf, expected_kind):
        ovs_driver = mock.Mock(spec=n_interface.OVSInterfaceDriver)
        ovs_driver.conf = conf
        ovs_driver = interface.MultiInterfaceDriver._process_driver_obj(
            ovs_driver)
        self.assertTrue(hasattr(ovs_driver, "_interface_kind_"))
        self.assertEqual(expected_kind, ovs_driver._interface_kind_)

    def test__process_driver_obj_ovs(self):
        conf = mock.Mock()
        conf.ovs_use_veth = False
        self._check_process_driver_obj_ovs(conf, "openvswitch")

    def test__process_driver_obj_ovs_veth(self):
        conf = mock.Mock()
        conf.ovs_use_veth = True
        self._check_process_driver_obj_ovs(conf, "veth")

    @mock.patch.object(n_ip_lib, "device_exists")
    def test__process_ipoib_driver_obj(self, dev_exist_mock):
        conf = mock.Mock()
        conf.ipoib_physical_interface = "ib0"
        ipoib_driver = interface.IPoIBInterfaceDriver(
            conf, get_networks_callback=mock.Mock())
        ipoib_driver = interface.MultiInterfaceDriver._process_driver_obj(
            ipoib_driver)
        self.assertTrue(hasattr(ipoib_driver, "_interface_kind_"))
        self.assertEqual('ipoib', ipoib_driver._interface_kind_)

    def test__process_unknown_driver_obj(self):
        unknown_driver = mock.Mock(spec=['conf'])
        unknown_driver = interface.MultiInterfaceDriver._process_driver_obj(
            unknown_driver)
        self.assertTrue(hasattr(unknown_driver, "_interface_kind_"))
        self.assertEqual('unknown', unknown_driver._interface_kind_)

    def test__get_networks_from_cache_all_nets(self):
        self._init_network_cache()
        nets = interface.MultiInterfaceDriver._get_networks_from_cache(
            filters=None, fields=None)
        for net in nets:
            # assert net object are identical
            self.assertDictEqual(network_db[net['id']], net)
        # assert all objects where retrieved
        self.assertEqual(set(network_db.keys()), set(
            [net['id'] for net in nets]))

    def test__get_networks_from_cache_some_fields(self):
        self._init_network_cache()
        some_fields = ['id', constants.PHYSICAL_NETWORK]
        nets = interface.MultiInterfaceDriver._get_networks_from_cache(
            filters=None, fields=some_fields)
        for net in nets:
            # assert retrieved net obj contains only the requested fields
            # 'id' field
            self.assertIn('id', net)
            self.assertIn(net['id'], network_db)
            # 'provider:physical_network' field
            self.assertIn(constants.PHYSICAL_NETWORK, net)
            self.assertEqual(
                network_db[net['id']][constants.PHYSICAL_NETWORK],
                net[constants.PHYSICAL_NETWORK])
            # No additional fields
            self.assertEqual(set(some_fields), set(net.keys()))
        # assert all networks were retrieved
        self.assertEqual(set(network_db.keys()), set(
            [net['id'] for net in nets]))

    def test__get_networks_from_cache_some_filters(self):
        self._init_network_cache()
        nets = interface.MultiInterfaceDriver._get_networks_from_cache(
            filters={'id': [uuids.vlan_net]}, fields=None)
        self.assertEqual(1, len(nets))
        self.assertDictEqual(network_db[uuids.vlan_net], nets[0])

    def test__get_networks_from_cache_invalid_field(self):
        self._init_network_cache()
        nets = interface.MultiInterfaceDriver._get_networks_from_cache(
            fields=['invalid'])
        self.assertEqual([], nets)

    def test__get_networks_from_cache_invalid_filter(self):
        self._init_network_cache()
        nets = interface.MultiInterfaceDriver._get_networks_from_cache(
            filters={'invalid': ['i', 'n', 'v', 'a', 'l', 'i', 'd']})
        self.assertEqual([], nets)

    @mock.patch.object(n_ip_lib, 'IPDevice')
    @mock.patch.object(interface.MultiInterfaceDriver, '_init_network_cache')
    @mock.patch.object(interface.MultiInterfaceDriver,
                       'load_interface_driver_mappings')
    def test__get_driver_for_existing_interface(self, load_mapping_mock,
                                                init_net_cache_mock,
                                                ip_device_mock):
        drivers = self._get_mock_drivers()
        load_mapping_mock.return_value = drivers
        ipd_mock = mock.Mock()
        ipd_mock.link.link_kind = 'ipoib'
        ip_device_mock.return_value = ipd_mock
        multi_driver = interface.MultiInterfaceDriver(mock.Mock, None)
        driver = multi_driver._get_driver_for_existing_interface("dev-name")
        self.assertEqual(drivers['ib-physnet'], driver)
        # in case a matching driver is not found - ensure None is returned
        ipd_mock.link.link_kind = 'veth'
        driver = multi_driver._get_driver_for_existing_interface("dummy-dev")
        self.assertIsNone(driver)

    @mock.patch.object(n_ip_lib, 'device_exists')
    @mock.patch.object(interface.MultiInterfaceDriver,
                       '_get_driver_for_existing_interface')
    @mock.patch.object(interface.MultiInterfaceDriver, '_init_network_cache')
    @mock.patch.object(interface.MultiInterfaceDriver,
                       'load_interface_driver_mappings')
    def _unplug_device(self, device_exists, load_mapping_mock,
                       init_net_cache_mock, get_driver_mock,
                       device_exists_mock):
        device_exists_mock.return_value = device_exists
        mock_driver = mock.Mock()
        get_driver_mock.return_value = mock_driver
        driver = interface.MultiInterfaceDriver(mock.Mock, None)
        driver.unplug("dummy-dev", namespace="dummy-ns")
        if device_exists:
            mock_driver.unplug.assert_called_once_with("dummy-dev", None,
                                                       "dummy-ns", None)
        else:
            mock_driver.unplug.assert_not_called()

    def test_unplug_device_exist(self):
        self._unplug_device(True)

    def test_unplug_device_does_not_exist(self):
        self._unplug_device(False)

    @mock.patch.object(interface.MultiInterfaceDriver,
                       '_get_driver_for_existing_interface')
    @mock.patch.object(interface.MultiInterfaceDriver, '_init_network_cache')
    @mock.patch.object(interface.MultiInterfaceDriver,
                       'load_interface_driver_mappings')
    def test_set_mtu(self, load_mapping_mock, init_net_cache_mock,
                     get_driver_mock):
        driver_mock = mock.Mock()
        get_driver_mock.return_value = driver_mock
        driver = interface.MultiInterfaceDriver(mock.Mock, None)
        driver.set_mtu("dummy-dev", 1500, namespace="dummy-ns", prefix=None)
        driver_mock.set_mtu.assert_called_once_with(
            "dummy-dev", 1500, "dummy-ns", None)

    @mock.patch.object(interface.MultiInterfaceDriver, '_init_network_cache')
    @mock.patch.object(interface.MultiInterfaceDriver,
                       'load_interface_driver_mappings')
    def test_plug_new(self, load_mapping_mock, init_net_cache_mock):
        self._init_network_cache()
        driver = interface.MultiInterfaceDriver(mock.Mock, None)
        driver.drivers = self._get_mock_drivers()
        device_name = 'test-dev'
        mac = 'fa:18:64:2d:07:d5'
        ns = 'test-ns'
        # network with physnet
        driver.plug_new(uuids.vlan_net, uuids.vlan_port, device_name, mac,
                        bridge=None, namespace=ns, prefix=None, mtu=None)
        driver.drivers['datacenter'].plug_new.assert_called_once_with(
            uuids.vlan_net, uuids.vlan_port, device_name, mac, None, ns, None,
            None)
        # network without physnet
        driver.plug_new(uuids.vxlan_net, uuids.vxlan_port, device_name, mac,
                        bridge=None, namespace=ns, prefix=None, mtu=None)
        driver.drivers['nil'].plug_new.assert_called_once_with(
            uuids.vxlan_net, uuids.vxlan_port, device_name, mac, None, ns,
            None, None)
