# Copyright 2014 Mellanox Technologies, Ltd
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

import mock
from neutron.tests import base
from oslo_config import cfg
import testtools

from networking_mlnx.plugins.ml2.drivers.mlnx.agent import (
    mlnx_eswitch_neutron_agent)
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import exceptions
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import utils


class TestEswichManager(base.BaseTestCase):

    def setUp(self):
        super(TestEswichManager, self).setUp()

        class MockEswitchUtils(object):
            def __init__(self, endpoint, timeout):
                pass

        mock.patch('networking_mlnx.plugins.mlnx.agent.utils.EswitchManager',
                   new=MockEswitchUtils)

        with mock.patch.object(utils, 'zmq'):
            self.manager = mlnx_eswitch_neutron_agent.EswitchManager(
                {}, None, None)

    def test_get_not_exist_port_id(self):
        with testtools.ExpectedException(exceptions.MlnxException):
            self.manager.get_port_id_by_pci('no-such-pci')


class TestMlnxEswitchRpcCallbacks(base.BaseTestCase):

    def setUp(self):
        super(TestMlnxEswitchRpcCallbacks, self).setUp()
        agent = mock.Mock()
        self.rpc_callbacks = \
            mlnx_eswitch_neutron_agent.MlnxEswitchRpcCallbacks(
                'context',
                agent,
                agent)

    def test_port_update(self):
        port = {
            'id': '806f1a00-77cf-4331-9c2d-93e4c321f31d',
            'mac_address': '10:20:30:40:50:60',
            'binding:profile': {'pci_slot': '0000:05:00.3'}
        }
        add_port_update = self.rpc_callbacks.agent.add_port_update
        self.rpc_callbacks.port_update('context', port=port)
        add_port_update.assert_called_once_with(
            (port['mac_address'], port['binding:profile']['pci_slot']))

    def test_port_update_no_pci(self):
        port = {
            'id': '806f1a00-77cf-4331-9c2d-93e4c321f31d',
            'mac_address': '10:20:30:40:50:60',
            'binding:profile': {}
        }
        add_port_update = self.rpc_callbacks.agent.add_port_update
        self.rpc_callbacks.port_update('context', port=port)
        self.assertFalse(add_port_update.called)


class TestEswitchAgent(base.BaseTestCase):
    VNIC_PORT_1 = ('10:20:30:40:50:60', '0000:06:00.2')
    VNIC_PORT_2 = ('11:21:31:41:51:61', '0000:06:00.3')
    VNIC_PORT_3 = ('12:22:32:42:52:62', '0000:06:00.4')
    VNIC_PORT_4 = ('13:23:33:43:53:63', '0000:06:00.5')
    VNIC_PORT_5 = ('14:24:34:44:54:64', '0000:06:00.6')
    VNIC_PORT_1_INVALID_MAC = ('ff:ff:ff:ff:ff:ff', '0000:06:00.2')
    VNIC_PORT_2_INVALID_MAC = ('00:00:00:00:00:00', '0000:06:00.3')

    def setUp(self):
        super(TestEswitchAgent, self).setUp()
        cfg.CONF.set_default('firewall_driver',
                             'noop',
                             group='SECURITYGROUP')

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        mock.patch('neutron.openstack.common.loopingcall.'
                   'FixedIntervalLoopingCall',
                   new=MockFixedIntervalLoopingCall)

        with mock.patch.object(utils, 'zmq'):
            self.agent = mlnx_eswitch_neutron_agent.MlnxEswitchNeutronAgent(
                {})
        self.agent.plugin_rpc = mock.Mock()
        self.agent.context = mock.Mock()
        self.agent.agent_id = mock.Mock()
        self.agent.eswitch = mock.Mock()
        self.agent.eswitch.get_vnics.return_value = []

    def test_treat_devices_added_returns_true_for_missing_device(self):
        attrs = {'get_devices_details_list.side_effect': Exception()}
        self.agent.plugin_rpc.configure_mock(**attrs)
        with mock.patch('networking_mlnx.plugins.ml2.drivers.mlnx.agent.'
                        'mlnx_eswitch_neutron_agent.EswitchManager.'
                        'get_vnics',
                        return_value=[]):
            self.assertTrue(self.agent.treat_devices_added_or_updated([{}]))

    def _mock_treat_devices_added_updated(self, details, func_name):
        """Mock treat devices added.

        :param details: the details to return for the device
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """

        with mock.patch('networking_mlnx.plugins.ml2.drivers.mlnx.agent.'
                        'mlnx_eswitch_neutron_agent.EswitchManager.'
                        'get_vnics',
                        return_value=[]),\
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[details]),\
            mock.patch.object(self.agent.plugin_rpc,
                              'update_device_up') as upd_dev_up,\
            mock.patch.object(self.agent.plugin_rpc,
                              'update_device_down') as upd_dev_down,\
            mock.patch.object(self.agent, func_name) as func:
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                [self.VNIC_PORT_1, self.VNIC_PORT_2]))
        return (func.called, upd_dev_up.called, upd_dev_down.called)

    def test_treat_devices_added_updates_known_port(self):
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        func, dev_up, dev_down = self._mock_treat_devices_added_updated(
                                 details, 'treat_vif_port')
        self.assertTrue(func)
        self.assertTrue(dev_up)

    def test_treat_devices_added_updates_known_port_admin_down(self):
        details = {'device': '01:02:03:04:05:06',
                   'network_id': '123456789',
                   'network_type': 'vlan',
                   'physical_network': 'default',
                   'segmentation_id': 2,
                   'admin_state_up': False}
        func, dev_up, dev_down = self._mock_treat_devices_added_updated(
                                     details, 'treat_vif_port')
        self.assertFalse(func)
        self.assertFalse(dev_up)
        self.assertTrue(dev_down)

    def test_treat_devices_added_updates_known_port_admin_up(self):
        details = {'port_id': '1234567890',
                   'device': '01:02:03:04:05:06',
                   'profile': {'pci_slot': '0000:06:00.2'},
                   'network_id': '123456789',
                   'network_type': 'vlan',
                   'physical_network': 'default',
                   'segmentation_id': 2,
                   'admin_state_up': False}
        func, dev_up, dev_down = self._mock_treat_devices_added_updated(
                                 details, 'treat_vif_port')
        self.assertTrue(func)
        self.assertTrue(dev_up)
        self.assertFalse(dev_down)

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_removed(
                set([self.VNIC_PORT_1])))

    def test_treat_devices_removed_releases_port(self):
        details = dict(exists=False)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent.eswitch,
                                   'port_release') as port_release:
                self.assertFalse(self.agent.treat_devices_removed(
                    set([self.VNIC_PORT_1])))
                self.assertTrue(port_release.called)

    def _test_process_network_ports(self, port_info):
        with mock.patch.object(self.agent,
                               'treat_devices_added_or_updated',
                               return_value=False) as device_added_updated,\
            mock.patch.object(self.agent, 'treat_devices_removed',
                              return_value=False) as device_removed:
            self.assertFalse(self.agent.process_network_ports(port_info))
            device_added_updated.assert_called_once_with(
                port_info['added'] | port_info['updated'])
            device_removed.assert_called_once_with(port_info['removed'])

    def test_process_network_ports(self):
        self._test_process_network_ports(
            {'current': set(self.VNIC_PORT_1),
             'updated': set(),
             'added': set(self.VNIC_PORT_2),
             'removed': set(self.VNIC_PORT_3)})

    def test_process_network_ports_with_updated_ports(self):
        self._test_process_network_ports(
            {'current': set(self.VNIC_PORT_1),
             'updated': set(self.VNIC_PORT_2),
             'added': set(self.VNIC_PORT_3),
             'removed': set(self.VNIC_PORT_4)})

    def test_add_port_update(self):
        self.agent.add_port_update(self.VNIC_PORT_1)
        self.assertEqual(set([self.VNIC_PORT_1]), self.agent.updated_ports)

    def _mock_scan_ports(self, vif_port_set, previous,
                         updated_ports, sync=False):
        self.agent.updated_ports = updated_ports
        with mock.patch.object(self.agent.eswitch, 'get_vnics',
                               return_value=vif_port_set):
            return self.agent.scan_ports(previous, sync)

    def test_scan_ports_return_current_for_unchanged_ports(self):
        vif_port_set = set([self.VNIC_PORT_1, self.VNIC_PORT_2])
        previous = dict(
            current=set([self.VNIC_PORT_1, self.VNIC_PORT_2]), added=set(),
            removed=set(), updated=set())
        expected = dict(current=vif_port_set, added=set(),
                        removed=set(), updated=set())
        actual = self._mock_scan_ports(vif_port_set,
                                       previous, set())
        self.assertEqual(expected, actual)

    def test_scan_ports_return_port_changes(self):
        vif_port_set = set([self.VNIC_PORT_1, self.VNIC_PORT_3])
        previous = dict(current=set([self.VNIC_PORT_1, self.VNIC_PORT_2]),
                        added=set(), removed=set(), updated=set())
        expected = dict(current=vif_port_set, added=set([self.VNIC_PORT_3]),
                        removed=set([self.VNIC_PORT_2]), updated=set())
        actual = self._mock_scan_ports(vif_port_set,
                                       previous, set())
        self.assertEqual(expected, actual)

    def test_scan_ports_with_updated_ports(self):
        vif_port_set = set(
            [self.VNIC_PORT_1, self.VNIC_PORT_3, self.VNIC_PORT_4])
        previous = dict(
            current=set([self.VNIC_PORT_1, self.VNIC_PORT_2,
                         self.VNIC_PORT_4]),
            added=set(),
            removed=set(),
            updated=set())
        expected = dict(
            current=vif_port_set,
            added=set([self.VNIC_PORT_3]),
            removed=set([self.VNIC_PORT_2]),
            updated=set([self.VNIC_PORT_4]))
        actual = self._mock_scan_ports(vif_port_set,
                                       previous, set([self.VNIC_PORT_4]))
        self.assertEqual(expected, actual)

    def test_scan_ports_with_unknown_updated_ports(self):
        vif_port_set = set(
            [self.VNIC_PORT_1, self.VNIC_PORT_3, self.VNIC_PORT_4])
        previous = dict(
            current=set([self.VNIC_PORT_1, self.VNIC_PORT_2,
                         self.VNIC_PORT_4]),
            added=set(),
            removed=set(),
            updated=set())
        expected = dict(current=vif_port_set,
                        added=set([self.VNIC_PORT_3]),
                        removed=set([self.VNIC_PORT_2]),
                        updated=set([self.VNIC_PORT_4]))
        actual = self._mock_scan_ports(
            vif_port_set, previous, updated_ports=set(
                [self.VNIC_PORT_4, self.VNIC_PORT_5]))
        self.assertEqual(expected, actual)

    def test_scan_ports_with_invalid_vif(self):
        vif_port_set = set(
            [self.VNIC_PORT_1, self.VNIC_PORT_2_INVALID_MAC, self.VNIC_PORT_4])
        previous = dict(
            current=set(
                [self.VNIC_PORT_1, self.VNIC_PORT_3, self.VNIC_PORT_4]),
            added=set(),
            removed=set(),
            updated=set())
        expected = dict(current=vif_port_set - {self.VNIC_PORT_2_INVALID_MAC},
                        added=set(),
                        removed=set([self.VNIC_PORT_3]),
                        updated=set([self.VNIC_PORT_1]))
        actual = self._mock_scan_ports(
            vif_port_set, previous, updated_ports=set([self.VNIC_PORT_1]))
        self.assertEqual(expected, actual)

    def test_fix_eswitchd_vnic_macs(self):
        vif_port_set = {self.VNIC_PORT_1_INVALID_MAC,
                        self.VNIC_PORT_2_INVALID_MAC, self.VNIC_PORT_3}
        updated_ports_cache = {self.VNIC_PORT_1[1]: self.VNIC_PORT_1[0]}
        fixed_vif_ports = self.agent.fix_eswitchd_vnic_macs(
            vif_port_set, updated_ports_cache)
        expected_vif_port_set = set([self.VNIC_PORT_1, self.VNIC_PORT_3])
        self.assertEqual(expected_vif_port_set, fixed_vif_ports)
