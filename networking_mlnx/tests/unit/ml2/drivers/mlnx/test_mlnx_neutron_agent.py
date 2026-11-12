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

import threading
import unittest.mock as mock

from neutron.tests import base
from oslo_config import cfg
import testtools

from networking_mlnx.plugins.ml2.drivers.mlnx.agent import (
    mlnx_eswitch_neutron_agent)
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import exceptions
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import utils

from oslo_service.backend._threading import loopingcall


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

    def test_port_up_does_not_enable_link(self):
        self.manager.utils = mock.Mock()
        self.manager.port_up('net-id', 'vlan', 'physnet', 4, 'port-id',
                             '10:20:30:40:50:60', '0000:06:00.2')
        self.manager.utils.set_port_vlan_id.assert_called_once_with(
            'physnet', 4, '0000:06:00.2')
        self.manager.utils.port_up.assert_not_called()

    def test_port_up_forgets_earlier_entries(self):
        self.manager.utils = mock.Mock()
        self.manager.port_up('net-1', 'vlan', 'physnet', 4, 'port-1',
                             '10:20:30:40:50:60', '0000:06:00.2')
        # The port moved to another VF, and another port got its old VF.
        self.manager.port_up('net-1', 'vlan', 'physnet', 4, 'port-1',
                             '10:20:30:40:50:60', '0000:06:00.3')
        self.manager.port_up('net-2', 'vlan', 'physnet', 5, 'port-2',
                             '11:21:31:41:51:61', '0000:06:00.2')
        self.assertEqual(
            [{'port_id': 'port-1', 'port_mac': '10:20:30:40:50:60',
              'pci_slot': '0000:06:00.3'}],
            self.manager.network_map['net-1']['ports'])
        self.assertEqual(
            [{'port_id': 'port-2', 'port_mac': '11:21:31:41:51:61',
              'pci_slot': '0000:06:00.2'}],
            self.manager.network_map['net-2']['ports'])

    def test_get_port_by_id(self):
        self.manager.network_map = {'net-id': {'ports': [
            {'port_id': 'port-id', 'port_mac': '10:20:30:40:50:60',
             'pci_slot': '0000:06:00.2'}]}}
        self.assertEqual(('10:20:30:40:50:60', '0000:06:00.2'),
                         self.manager.get_port_by_id('port-id'))
        self.assertIsNone(self.manager.get_port_by_id('other-id'))


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

    def test_binding_activate(self):
        agent = self.rpc_callbacks.agent
        agent.conf.host = 'host1'
        # The VF changed since the port was configured on this host.
        agent.eswitch.get_port_by_id.return_value = (
            '10:20:30:40:50:60', '0000:05:00.3')
        agent.eswitch.get_vnics.return_value = {
            ('10:20:30:40:50:60', '0000:05:00.4'),
            ('11:21:31:41:51:61', '0000:05:00.3')}
        self.rpc_callbacks.binding_activate('context', port_id='port-id',
                                            host='host1')
        agent.add_port_update.assert_called_once_with(
            ('10:20:30:40:50:60', '0000:05:00.4'))

    def test_binding_activate_port_with_inactive_binding(self):
        # The VF was attached while the port was still bound to the source
        # host, so the agent didn't configure it.
        agent = self.rpc_callbacks.agent
        agent.conf.host = 'host1'
        agent.eswitch.get_port_by_id.return_value = None
        device = ('10:20:30:40:50:60', '0000:05:00.4')
        agent.get_inactive_binding_devices.return_value = {device}
        self.rpc_callbacks.binding_activate('context', port_id='port-id',
                                            host='host1')
        agent.add_port_update.assert_called_once_with(device)

    def test_binding_activate_port_not_attached_yet(self):
        # The VF is attached after the binding is activated, and then added.
        agent = self.rpc_callbacks.agent
        agent.conf.host = 'host1'
        agent.eswitch.get_port_by_id.return_value = None
        agent.get_inactive_binding_devices.return_value = set()
        self.rpc_callbacks.binding_activate('context', port_id='port-id',
                                            host='host1')
        agent.add_port_update.assert_not_called()

    def test_binding_activate_other_host(self):
        agent = self.rpc_callbacks.agent
        agent.conf.host = 'host1'
        self.rpc_callbacks.binding_activate('context', port_id='port-id',
                                            host='host2')
        agent.add_port_update.assert_not_called()

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
        mock.patch('oslo_service.backend._threading.loopingcall.'
                   'FixedIntervalLoopingCall',
                   new=MockFixedIntervalLoopingCall)
        self.mock_loopstart_p = mock.patch.object(
            loopingcall.FixedIntervalLoopingCall, 'start')
        self.mock_loopstart = self.mock_loopstart_p.start()

        with mock.patch.object(utils, 'zmq'):
            self.agent = mlnx_eswitch_neutron_agent.MlnxEswitchNeutronAgent(
                {})
        self.agent.plugin_rpc = mock.Mock()
        self.agent.context = mock.Mock()
        self.agent.agent_id = mock.Mock()
        self.agent.eswitch = mock.Mock()
        self.agent.eswitch.get_vnics.return_value = []
        self.agent.eswitch.get_plugged_vnics.return_value = {}
        self.agent.eswitch.find_port_id_by_pci.return_value = None

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
                        return_value=[]), \
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[details]), \
            mock.patch.object(self.agent.plugin_rpc,
                              'update_device_up') as upd_dev_up, \
            mock.patch.object(self.agent.plugin_rpc,
                              'update_device_down') as upd_dev_down, \
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
                               return_value=False) as device_added_updated, \
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

    def test_port_update_during_scan_is_not_lost(self):
        adding = threading.Event()
        resume = threading.Event()

        class PausingSet(set):
            # Pause the RPC thread after it has loaded updated_ports but
            # before its add() takes effect.
            def add(self, item):
                adding.set()
                resume.wait(10)
                super(PausingSet, self).add(item)

        vnics = set([self.VNIC_PORT_1, self.VNIC_PORT_2])
        self.agent.eswitch.get_vnics.return_value = vnics
        self.agent.updated_ports = PausingSet()
        previous = dict(current=vnics, added=set(), removed=set(),
                        updated=set())
        rpc = threading.Thread(target=self.agent.add_port_update,
                               args=(self.VNIC_PORT_2,))
        rpc.start()
        self.assertTrue(adding.wait(10))

        results = []
        scan = threading.Thread(
            target=lambda: results.append(
                self.agent.scan_ports(previous, sync=False)))
        scan.start()
        scan.join(0.1)
        resume.set()
        rpc.join()
        scan.join()

        # The update must be seen by this scan or the next one.
        next_info = self.agent.scan_ports(results[0], sync=False)
        self.assertEqual(set([self.VNIC_PORT_2]),
                         results[0]['updated'] | next_info['updated'])

    def _mock_scan_ports(self, vif_port_set, previous,
                         updated_ports, sync=False):
        self.agent.updated_ports = updated_ports
        with mock.patch.object(self.agent.eswitch, 'get_vnics',
                               return_value=vif_port_set):
            return self.agent.scan_ports(previous, sync)

    def test_scan_ports_keeps_updates_when_get_vnics_fails(self):
        self.agent.updated_ports = {self.VNIC_PORT_1}
        self.agent.eswitch.get_vnics.side_effect = exceptions.OperationFailed(
            err_msg='failed')
        previous = dict(current=set(), added=set(), removed=set(),
                        updated=set())
        self.assertRaises(exceptions.OperationFailed,
                          self.agent.scan_ports, previous, False)
        self.assertEqual({self.VNIC_PORT_1}, self.agent.updated_ports)

    def test_run_survives_scan_failure(self):
        class StopLoop(BaseException):
            pass

        scans = []

        def scan_ports(previous, sync):
            scans.append(sync)
            if len(scans) == 2:
                raise exceptions.OperationFailed(err_msg='failed')
            if len(scans) == 3:
                raise StopLoop()
            return previous

        with mock.patch.object(self.agent, 'scan_ports',
                               side_effect=scan_ports), \
                mock.patch.object(self.agent, '_sync_link_states'), \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'sleep') as sleep:
            self.assertRaises(StopLoop, self.agent.run)
        # After the failed scan, the loop waits, then scans again,
        # resyncing with the server.
        sleep.assert_any_call(self.agent._polling_interval)
        self.assertEqual([True, False, True], scans)

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

    def _port(self, vnic, status='ACTIVE', host='host1', admin_state_up=True):
        mac, pci_slot = vnic
        return {'mac_address': mac, 'status': status,
                'admin_state_up': admin_state_up,
                'binding:host_id': host,
                'binding:profile': {'pci_slot': pci_slot}}

    def _sync_link_states(self, vnics, ports):
        self.agent.conf.host = 'host1'
        self.agent.eswitch.get_vnics.return_value = set(vnics)
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.return_value = (
            ports)
        self.agent.eswitch.set_link.reset_mock()
        self.agent._sync_link_states()
        return {c[0][0]: c[0][1] if len(c[0]) > 1 else None
                for c in self.agent.eswitch.set_link.call_args_list}

    def test_sync_link_states_enables_only_ready_ports(self):
        calls = self._sync_link_states(
            [self.VNIC_PORT_1, self.VNIC_PORT_2, self.VNIC_PORT_3,
             self.VNIC_PORT_4, self.VNIC_PORT_5],
            [self._port(self.VNIC_PORT_1),
             self._port(self.VNIC_PORT_2, status='DOWN'),
             self._port(self.VNIC_PORT_3, status='ERROR'),
             self._port(self.VNIC_PORT_4, host='host2'),
             self._port(self.VNIC_PORT_5, admin_state_up=False)])
        self.assertEqual({self.VNIC_PORT_1[1]: self.VNIC_PORT_1[0],
                          self.VNIC_PORT_2[1]: None,
                          self.VNIC_PORT_3[1]: None,
                          self.VNIC_PORT_4[1]: None,
                          self.VNIC_PORT_5[1]: None}, calls)

    def test_sync_link_states_disables_vf_with_other_guid(self):
        # The VF still has another port's GUID, or an invalid one.
        port = self._port(self.VNIC_PORT_1)
        for vnic in (self.VNIC_PORT_2, self.VNIC_PORT_1_INVALID_MAC):
            vnic = (vnic[0], self.VNIC_PORT_1[1])
            self.agent.link_states.clear()
            calls = self._sync_link_states([vnic], [port])
            self.assertEqual({self.VNIC_PORT_1[1]: None}, calls)

    def test_sync_link_states_sets_links_every_time(self):
        # Nova may plug a VF again, which disables its link, between two
        # iterations.
        vnics = [self.VNIC_PORT_1]
        self._sync_link_states(vnics, [self._port(self.VNIC_PORT_1)])
        self.assertEqual({self.VNIC_PORT_1[1]: self.VNIC_PORT_1[0]},
                         self._sync_link_states(
                             vnics, [self._port(self.VNIC_PORT_1)]))
        # The port is no longer ACTIVE, for example after a binding change.
        self.assertEqual({self.VNIC_PORT_1[1]: None}, self._sync_link_states(
            vnics, [self._port(self.VNIC_PORT_1, status='DOWN')]))

    def test_sync_link_states_disables_vf_that_is_gone(self):
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self.assertEqual({self.VNIC_PORT_1[1]: None},
                         self._sync_link_states([], []))
        self.assertEqual({}, self.agent.link_states)

    def test_sync_link_states_after_vf_gets_another_port(self):
        pci_slot = self.VNIC_PORT_1[1]
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        # Nova plugged the VF for another port, which disabled its link.
        vnic = (self.VNIC_PORT_2[0], pci_slot)
        self.assertEqual({pci_slot: vnic[0]}, self._sync_link_states(
            [vnic], [self._port(vnic)]))

    def test_sync_link_states_rpc_failure_keeps_only_own_links(self):
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.side_effect = (
            Exception)
        # VNIC_PORT_2 is a VF that this agent didn't enable, for example
        # after it restarted, or after Nova failed to plug it.
        calls = self._sync_link_states(
            [self.VNIC_PORT_1, self.VNIC_PORT_2], [])
        self.assertEqual({self.VNIC_PORT_1[1]: self.VNIC_PORT_1[0],
                          self.VNIC_PORT_2[1]: None}, calls)

    def test_sync_link_states_retries_after_failure(self):
        self.agent.eswitch.set_link.side_effect = [Exception, None]
        vnics = [self.VNIC_PORT_1]
        self._sync_link_states(vnics, [self._port(self.VNIC_PORT_1)])
        self.assertNotIn(self.VNIC_PORT_1[1], self.agent.link_states)
        self.agent.eswitch.set_link.side_effect = None
        self.assertEqual({self.VNIC_PORT_1[1]: self.VNIC_PORT_1[0]},
                         self._sync_link_states(
                             vnics, [self._port(self.VNIC_PORT_1)]))

    def _set_link_guid_mismatch(self, pci_slot, disable_error=None):
        def set_link(slot, mac=None):
            if slot == pci_slot:
                if mac:
                    raise exceptions.GuidMismatch(err_msg='wrong GUID')
                if disable_error:
                    raise disable_error

        self.agent.eswitch.set_link.side_effect = set_link

    def test_sync_link_states_guid_mismatch_disables_link(self):
        # Nova has released the VF of VNIC_PORT_1 from the instance, so it
        # no longer has the port's GUID, while the port is still ACTIVE.
        mac, pci_slot = self.VNIC_PORT_1
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])

        self._set_link_guid_mismatch(pci_slot)
        with mock.patch.object(mlnx_eswitch_neutron_agent, 'LOG') as log:
            self._sync_link_states(
                [self.VNIC_PORT_1, self.VNIC_PORT_2],
                [self._port(self.VNIC_PORT_1), self._port(self.VNIC_PORT_2)])
        self.assertEqual(
            [mock.call(pci_slot, mac), mock.call(pci_slot)],
            [c for c in self.agent.eswitch.set_link.call_args_list
             if c[0][0] == pci_slot])
        log.warning.assert_called_once()
        log.exception.assert_not_called()
        self.assertEqual((mac, False), self.agent.link_states[pci_slot])
        self.assertEqual((self.VNIC_PORT_2[0], True),
                         self.agent.link_states[self.VNIC_PORT_2[1]])

    def test_sync_link_states_enables_link_again_after_guid_mismatch(self):
        # Nova plugged the VF for the same port again, for example on a
        # hard reboot.
        mac, pci_slot = self.VNIC_PORT_1
        self._set_link_guid_mismatch(pci_slot)
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self.agent.eswitch.set_link.side_effect = None
        with mock.patch.object(mlnx_eswitch_neutron_agent, 'LOG') as log:
            self.assertEqual({pci_slot: mac}, self._sync_link_states(
                [self.VNIC_PORT_1], [self._port(self.VNIC_PORT_1)]))
        self.assertEqual((mac, True), self.agent.link_states[pci_slot])
        log.info.assert_called_once()

    def test_sync_link_states_guid_mismatch_while_rpc_fails(self):
        # Only links that this agent enabled stay up while the ports can't
        # be fetched, and a VF that lost the GUID is disabled.
        mac, pci_slot = self.VNIC_PORT_1
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.side_effect = (
            Exception)
        self._set_link_guid_mismatch(pci_slot)
        self._sync_link_states([self.VNIC_PORT_1], [])
        self.assertEqual(
            [mock.call(pci_slot, mac), mock.call(pci_slot)],
            self.agent.eswitch.set_link.call_args_list)
        self.assertEqual((mac, False), self.agent.link_states[pci_slot])
        # It stays disabled while the ports still can't be fetched.
        self.agent.eswitch.set_link.side_effect = None
        self.assertEqual({pci_slot: None}, self._sync_link_states(
            [self.VNIC_PORT_1], []))

    def test_sync_link_states_guid_mismatch_retries_disable(self):
        mac, pci_slot = self.VNIC_PORT_1
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self._set_link_guid_mismatch(pci_slot, disable_error=Exception())
        self._sync_link_states([self.VNIC_PORT_1],
                               [self._port(self.VNIC_PORT_1)])
        self.assertEqual((mac, False), self.agent.link_states[pci_slot])

        # Nova has unplugged the VF, but without disabling its link.
        self.agent.eswitch.set_link.side_effect = None
        self.assertEqual({pci_slot: None}, self._sync_link_states([], []))
        self.assertNotIn(pci_slot, self.agent.link_states)

    def test_treat_devices_added_failure_does_not_report_port_up(self):
        details = [{'device': mac, 'port_id': 'port-%s' % mac,
                    'profile': {'pci_slot': pci_slot},
                    'network_id': 'net-id', 'network_type': 'vlan',
                    'physical_network': 'physnet', 'segmentation_id': 4,
                    'admin_state_up': True}
                   for mac, pci_slot in (self.VNIC_PORT_1, self.VNIC_PORT_2)]
        self.agent.plugin_rpc.get_devices_details_list.return_value = details
        self.agent.link_states[self.VNIC_PORT_1[1]] = (
            self.VNIC_PORT_1[0], True)
        with mock.patch.object(self.agent, 'treat_vif_port',
                               side_effect=[Exception, None]):
            self.assertTrue(self.agent.treat_devices_added_or_updated(
                [self.VNIC_PORT_1, self.VNIC_PORT_2]))
        self.agent.plugin_rpc.update_device_up.assert_called_once_with(
            self.agent.context, self.VNIC_PORT_2[0], self.agent.agent_id,
            self.agent.conf.host)
        self.assertNotIn(self.VNIC_PORT_1[1], self.agent.link_states)

    def test_process_network_ports_releases_before_configuring(self):
        # Nova gave a VF to another port within one iteration.
        port_info = {'current': {self.VNIC_PORT_2}, 'updated': set(),
                     'added': {(self.VNIC_PORT_2[0], self.VNIC_PORT_1[1])},
                     'removed': {self.VNIC_PORT_1}}
        manager = mock.Mock()
        manager.removed.return_value = False
        manager.added.return_value = False
        with mock.patch.object(self.agent, 'treat_devices_removed',
                               manager.removed), \
                mock.patch.object(self.agent,
                                  'treat_devices_added_or_updated',
                                  manager.added):
            self.agent.process_network_ports(port_info)
        self.assertEqual(['removed', 'added'],
                         [c[0] for c in manager.mock_calls])

    def test_treat_devices_added_no_active_binding(self):
        # Neutron's answer for a port live migrated to this host whose
        # binding here isn't active yet.
        self.agent.plugin_rpc.get_devices_details_list.return_value = [
            {'device': self.VNIC_PORT_1[0], 'no_active_binding': True}]
        with mock.patch.object(self.agent, 'treat_vif_port') as treat:
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                [self.VNIC_PORT_1]))
        treat.assert_not_called()
        self.agent.plugin_rpc.update_device_up.assert_not_called()
        self.agent.plugin_rpc.update_device_down.assert_not_called()
        self.assertEqual({self.VNIC_PORT_1},
                         self.agent.get_inactive_binding_devices())

        # Once the binding is active, the port is configured.
        details = {'device': self.VNIC_PORT_1[0], 'port_id': 'port-id',
                   'profile': {'pci_slot': self.VNIC_PORT_1[1]},
                   'network_id': 'net-id', 'network_type': 'vlan',
                   'physical_network': 'physnet', 'segmentation_id': 4,
                   'admin_state_up': True}
        self.agent.plugin_rpc.get_devices_details_list.return_value = [
            details]
        with mock.patch.object(self.agent, 'treat_vif_port') as treat:
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                [self.VNIC_PORT_1]))
        treat.assert_called_once()
        self.agent.plugin_rpc.update_device_up.assert_called_once_with(
            self.agent.context, self.VNIC_PORT_1[0], self.agent.agent_id,
            self.agent.conf.host)
        self.assertEqual(set(), self.agent.get_inactive_binding_devices())

    def test_binding_activate_after_no_active_binding(self):
        callbacks = mlnx_eswitch_neutron_agent.MlnxEswitchRpcCallbacks(
            'context', self.agent, None)
        self.agent.conf.host = 'host1'
        self.agent.plugin_rpc.get_devices_details_list.return_value = [
            {'device': self.VNIC_PORT_1[0], 'no_active_binding': True}]
        self.agent.eswitch.get_vnics.return_value = {self.VNIC_PORT_1}
        previous = self.agent.scan_ports(
            dict(current=set(), added=set(), removed=set(), updated=set()),
            sync=False)
        self.assertEqual({self.VNIC_PORT_1}, previous['added'])
        self.agent.process_network_ports(previous)

        self.agent.eswitch.get_port_by_id.return_value = None
        callbacks.binding_activate('context', port_id='port-id',
                                   host='host1')

        # The VF is processed again although it is still attached.
        port_info = self.agent.scan_ports(previous, sync=False)
        self.assertEqual({self.VNIC_PORT_1}, port_info['updated'])

    def test_treat_devices_removed_no_active_binding(self):
        self.agent._set_inactive_binding(self.VNIC_PORT_1, True)
        self.assertFalse(self.agent.treat_devices_removed(
            {self.VNIC_PORT_1}))
        self.agent.plugin_rpc.update_device_down.assert_not_called()
        self.agent.eswitch.port_release.assert_not_called()
        self.assertEqual(set(), self.agent.get_inactive_binding_devices())

    def test_treat_devices_removed_no_active_binding_configured_vf(self):
        # The agent configured a port on the VF earlier.
        self.agent._set_inactive_binding(self.VNIC_PORT_1, True)
        self.agent.eswitch.find_port_id_by_pci.return_value = 'port-id'
        self.agent.eswitch.get_port_id_by_pci.return_value = 'port-id'
        self.agent.plugin_rpc.update_device_down.return_value = {
            'exists': True}
        self.assertFalse(self.agent.treat_devices_removed(
            {self.VNIC_PORT_1}))
        self.agent.plugin_rpc.update_device_down.assert_called_once()
        self.agent.eswitch.port_release.assert_called_once_with(
            self.VNIC_PORT_1[1])

    def test_recheck_inactive_bindings(self):
        self.agent.conf.host = 'host1'
        self.agent._set_inactive_binding(self.VNIC_PORT_1, True)
        self.agent._set_inactive_binding(self.VNIC_PORT_2, True)
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.return_value = [
            self._port((self.VNIC_PORT_1[0].upper(), self.VNIC_PORT_1[1]),
                       status='DOWN'),
            self._port(self.VNIC_PORT_2, host='host2')]
        self.agent._recheck_inactive_bindings()
        self.assertEqual({self.VNIC_PORT_1}, self.agent.updated_ports)

    def test_ports_on_host_failure_is_cached(self):
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.side_effect = (
            ValueError)
        ports_on_host = mlnx_eswitch_neutron_agent.PortsOnHost(self.agent)
        self.assertRaises(ValueError, ports_on_host.get)
        # Later callers in the iteration don't log the same traceback again.
        self.assertRaises(mlnx_eswitch_neutron_agent.PortsUnavailable,
                          ports_on_host.get)
        self.assertEqual(
            1, self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.
            call_count)

    def test_sync_link_states_and_recheck_share_ports(self):
        self.agent.conf.host = 'host1'
        self.agent.eswitch.get_vnics.return_value = {self.VNIC_PORT_1}
        self.agent._set_inactive_binding(self.VNIC_PORT_2, True)
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.return_value = [
            self._port(self.VNIC_PORT_1), self._port(self.VNIC_PORT_2)]
        ports_on_host = mlnx_eswitch_neutron_agent.PortsOnHost(self.agent)
        self.agent._sync_link_states(ports_on_host)
        self.agent._recheck_inactive_bindings(ports_on_host)
        self.assertEqual(
            1, self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.
            call_count)
        self.agent.eswitch.set_link.assert_called_once_with(
            self.VNIC_PORT_1[1], self.VNIC_PORT_1[0])
        self.assertEqual({self.VNIC_PORT_2}, self.agent.updated_ports)

    def test_sync_link_states_ports_unavailable(self):
        # Another caller in the iteration already logged the error.
        self.agent.link_states[self.VNIC_PORT_1[1]] = (
            self.VNIC_PORT_1[0], True)
        self.agent.eswitch.get_vnics.return_value = {self.VNIC_PORT_1}
        ports_on_host = mock.Mock()
        ports_on_host.get.side_effect = (
            mlnx_eswitch_neutron_agent.PortsUnavailable('failed'))
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'exception') as log_exception:
            self.agent._sync_link_states(ports_on_host)
        log_exception.assert_not_called()
        self.agent.eswitch.set_link.assert_called_once_with(
            self.VNIC_PORT_1[1], self.VNIC_PORT_1[0])

    def test_recheck_inactive_bindings_without_devices(self):
        self.agent._recheck_inactive_bindings()
        self.agent.plugin_rpc.get_ports_by_vnic_type_and_host.\
            assert_not_called()


class TestPluggedVnics(base.BaseTestCase):
    MAC = 'fa:16:3e:a6:71:17'
    PCI_SLOT = '0000:06:00.2'

    def setUp(self):
        super(TestPluggedVnics, self).setUp()
        cfg.CONF.set_default('firewall_driver', 'noop',
                             group='SECURITYGROUP')
        mock.patch.object(loopingcall.FixedIntervalLoopingCall,
                          'start').start()
        with mock.patch.object(utils, 'zmq'):
            self.agent = mlnx_eswitch_neutron_agent.MlnxEswitchNeutronAgent(
                {})
        self.agent.conf.host = 'host1'
        self.agent.plugin_rpc = mock.Mock()
        self.agent.context = mock.Mock()
        self.agent.agent_id = 'agent-id'
        self.agent.eswitch = mock.Mock()
        self.agent.eswitch.get_vnics.return_value = set()
        self.rpc = self.agent.plugin_rpc
        self.plug('plug-1')
        self.ports([self._port()])

    def plug(self, plug_id, mac=MAC, pci_slot=PCI_SLOT, guid_checked=True):
        self.agent.eswitch.get_plugged_vnics.return_value = {
            pci_slot: {'mac': mac, 'pci_slot': pci_slot,
                       'plug_id': plug_id, 'guid_checked': guid_checked}}

    def ports(self, ports):
        self.rpc.get_ports_by_vnic_type_and_host.return_value = ports

    def _port(self, port_id='port-1', mac=MAC, host='host2',
              migrating_to='host1', pci_slot='0000:81:00.3'):
        # The port dict has the active binding's host and profile.
        profile = {}
        if pci_slot:
            profile['pci_slot'] = pci_slot
        if migrating_to:
            profile['migrating_to'] = migrating_to
        return {'id': port_id, 'mac_address': mac, 'status': 'ACTIVE',
                'admin_state_up': True, 'binding:host_id': host,
                'binding:profile': profile}

    def assert_notified(self, *port_ids):
        self.assertEqual(
            [mock.call(self.agent.context, port_id, 'agent-id', 'host1')
             for port_id in port_ids],
            self.rpc.update_device_up.call_args_list)

    def test_notify(self):
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)
        # The VF isn't configured and its link isn't enabled.
        self.assertEqual([mock.call.get_plugged_vnics([])],
                         self.agent.eswitch.mock_calls)
        self.assertEqual({}, self.agent.link_states)
        self.rpc.update_device_down.assert_not_called()

    def test_notify_once_per_plug(self):
        self.agent._notify_plugged_vnics()
        # The ports of the iteration, then again just before reporting.
        self.assertEqual(
            2, self.rpc.get_ports_by_vnic_type_and_host.call_count)
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')
        # Nothing awaits notification, so the ports aren't fetched again.
        self.assertEqual(
            2, self.rpc.get_ports_by_vnic_type_and_host.call_count)

    def test_binding_activated_before_report(self):
        # Nova activated the binding on this host after the agent got the
        # ports of the iteration. Reporting the port up now would complete
        # its provisioning before the VF is configured.
        self.rpc.get_ports_by_vnic_type_and_host.side_effect = [
            [self._port()],
            [self._port(host='host1', pci_slot=self.PCI_SLOT)]]
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)

    def test_binding_deleted_before_report(self):
        # Nova rolled back the migration and deleted the binding here.
        self.rpc.get_ports_by_vnic_type_and_host.side_effect = [
            [self._port()], []]
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)

    def test_fresh_ports_failure(self):
        self.rpc.get_ports_by_vnic_type_and_host.side_effect = [
            [self._port()], ValueError]
        self.assertRaises(ValueError, self.agent._notify_plugged_vnics)
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)

    def test_notify_again_after_plug_again(self):
        # For example a retried live migration to the same VF.
        self.agent._notify_plugged_vnics()
        self.plug('plug-2')
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1', 'port-1')
        self.assertEqual({self.PCI_SLOT: 'plug-2'}, self.agent.handled_plugs)

    def test_unplug_forgets_plug(self):
        self.agent._notify_plugged_vnics()
        # Nova unplugged the VF, for example when it rolled back the
        # migration, or the VF's GUID changed.
        self.agent.eswitch.get_plugged_vnics.return_value = {}
        self.agent._notify_plugged_vnics()
        self.assertEqual({}, self.agent.handled_plugs)
        # A later migration of the same port to the same VF.
        self.plug('plug-2')
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1', 'port-1')

    def test_mac_is_case_insensitive(self):
        self.ports([self._port(mac=self.MAC.upper())])
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')

    def test_port_bound_to_this_host(self):
        # The usual boot or attach path, which reports the port up once the
        # VF is attached.
        self.ports([self._port(host='host1', migrating_to=None,
                               pci_slot=self.PCI_SLOT)])
        self.agent._notify_plugged_vnics()
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)
        self.assertEqual(
            1, self.rpc.get_ports_by_vnic_type_and_host.call_count)

    def test_port_bound_to_this_host_with_stale_migrating_to(self):
        self.ports([self._port(host='host1', pci_slot=self.PCI_SLOT)])
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)

    def test_same_mac_port_bound_here_before_migrating_to(self):
        # A port of another mechanism driver with the same mac address is
        # bound to this host with another VF. Nova sets migrating_to on the
        # migrating port just after it plugs the VF.
        other = self._port(port_id='port-2', host='host1', migrating_to=None,
                           pci_slot='0000:06:00.7')
        self.ports([other, self._port(migrating_to=None)])
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)
        self.ports([other, self._port()])
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)

    def test_port_bound_here_without_this_vf(self):
        # A port bound to this host whose profile has no pci_slot, another
        # one, or no profile at all, isn't the one the VF was plugged for.
        window = mlnx_eswitch_neutron_agent.PLUGGED_VNIC_MATCH_WINDOW
        no_profile = dict(self._port(host='host1', migrating_to=None),
                          **{'binding:profile': None})
        cases = {'none': self._port(host='host1', migrating_to=None,
                                    pci_slot=None),
                 'other': self._port(host='host1', migrating_to=None,
                                     pci_slot='0000:06:00.7'),
                 'no-profile': no_profile}
        for case, port in cases.items():
            with self.subTest(pci_slot=case):
                self.agent.handled_plugs.clear()
                self.agent._unmatched_plugs.clear()
                self.ports([port])
                self._notify_at(1000)
                self.assertEqual({}, self.agent.handled_plugs)
                self.assertIn(self.PCI_SLOT, self.agent._unmatched_plugs)
                self._notify_at(1000 + window)
                self.assertEqual({self.PCI_SLOT: 'plug-1'},
                                 self.agent.handled_plugs)
                self.assert_notified()

    def test_no_binding_on_this_host(self):
        # get_ports_by_vnic_type_and_host only returns ports with a binding
        # on this host, so a port without one is never among them: either
        # no port, or only ports with other mac addresses.
        for ports in ([], [self._port(mac='fa:16:3e:00:00:01')]):
            self.ports(ports)
            self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)
        self.assertIn(self.PCI_SLOT, self.agent._unmatched_plugs)

    def test_migrating_elsewhere(self):
        for migrating_to in (None, 'host3'):
            self.ports([self._port(migrating_to=migrating_to)])
            self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)

    def test_migrating_to_set_after_plug(self):
        # Nova sets migrating_to just after it plugs the VF.
        self.ports([self._port(migrating_to=None)])
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.ports([self._port()])
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')

    def test_unbound_port(self):
        self.ports([self._port(host='')])
        self.agent._notify_plugged_vnics()
        self.assert_notified()

    def test_ambiguous_mac(self):
        # Ports on two networks can have the same mac address.
        self.ports([self._port(), self._port(port_id='port-2')])
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'warning') as warning:
            self.agent._notify_plugged_vnics()
        warning.assert_called_once()
        self.assertEqual(['port-1', 'port-2'],
                         sorted(warning.call_args[0][1]['ports']))
        self.assert_notified()
        # The plug isn't handled, so it is looked at again.
        self.assertEqual({}, self.agent.handled_plugs)
        self.assertIn(self.PCI_SLOT, self.agent._unmatched_plugs)

    def test_guid_changed(self):
        # eswitchd doesn't list a VF that no longer has the plugged GUID.
        self.agent.eswitch.get_plugged_vnics.return_value = {}
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.rpc.get_ports_by_vnic_type_and_host.assert_not_called()

    def test_rpc_failure_is_retried(self):
        self.rpc.update_device_up.side_effect = [Exception, None]
        self.agent._notify_plugged_vnics()
        self.assertEqual({}, self.agent.handled_plugs)
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1', 'port-1')
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)

    def test_get_ports_failure(self):
        self.rpc.get_ports_by_vnic_type_and_host.side_effect = ValueError
        self.assertRaises(ValueError, self.agent._notify_plugged_vnics)
        self.assert_notified()
        self.assertEqual({}, self.agent.handled_plugs)

    def test_old_eswitchd(self):
        self.agent.eswitch.get_plugged_vnics.side_effect = (
            exceptions.UnsupportedAction(action='get_plugged_vnics'))
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'warning') as warning, \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'monotonic', return_value=1000):
            self.agent._notify_plugged_vnics()
            self.agent._notify_plugged_vnics()
        warning.assert_called_once()
        # eswitchd isn't asked again for a while.
        self.assertEqual(1, self.agent.eswitch.get_plugged_vnics.call_count)
        self.assert_notified()
        self.rpc.get_ports_by_vnic_type_and_host.assert_not_called()

        # eswitchd was upgraded and restarted since.
        self.agent.eswitch.get_plugged_vnics.side_effect = None
        later = 1000 + mlnx_eswitch_neutron_agent.PLUGGED_VNICS_RETRY_INTERVAL
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'warning') as warning, \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'monotonic', return_value=later):
            self.agent._notify_plugged_vnics()
        warning.assert_not_called()
        self.assert_notified('port-1')

    def test_eswitchd_failure(self):
        self.agent.eswitch.get_plugged_vnics.side_effect = (
            exceptions.OperationFailed(err_msg='failed'))
        self.assertRaises(exceptions.OperationFailed,
                          self.agent._notify_plugged_vnics)
        self.assertIsNone(self.agent._plugged_vnics_retry_at)

    def test_iteration_ports_fetched_once_plus_before_report(self):
        vnic = ('fa:16:3e:00:00:01', '0000:06:00.3')
        self.agent.eswitch.get_vnics.return_value = {vnic}
        self.ports([self._port(), dict(self._port(port_id='port-2',
                                                  mac=vnic[0], host='host1'),
                                       **{'binding:profile': {
                                           'pci_slot': vnic[1]}})])
        self.agent._set_inactive_binding(('fa:16:3e:00:00:02', vnic[1]),
                                         True)

        class StopLoop(BaseException):
            pass

        with mock.patch.object(self.agent, 'scan_ports',
                               side_effect=[{'current': {vnic},
                                             'added': set(),
                                             'removed': set(),
                                             'updated': set()},
                                            StopLoop]), \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'sleep'):
            self.assertRaises(StopLoop, self.agent.run)
        # Once for the iteration, and once just before the report.
        self.assertEqual(
            2, self.rpc.get_ports_by_vnic_type_and_host.call_count)
        self.agent.eswitch.set_link.assert_called_once_with(vnic[1], vnic[0])
        self.assert_notified('port-1')

    def test_ports_not_fetched_without_vnics_or_pending_plugs(self):
        self.agent.eswitch.get_plugged_vnics.return_value = {}
        ports_on_host = mlnx_eswitch_neutron_agent.PortsOnHost(self.agent)
        self.agent._sync_link_states(ports_on_host)
        self.agent._recheck_inactive_bindings(ports_on_host)
        self.agent._notify_plugged_vnics(ports_on_host)
        self.rpc.get_ports_by_vnic_type_and_host.assert_not_called()

    def test_sync_link_states_ignores_plugged_vnics(self):
        # The port is ACTIVE on the source host, so its VF on this host is
        # disabled even after the agent reported it plugged.
        vnic = (self.MAC, self.PCI_SLOT)
        self.agent.eswitch.get_vnics.return_value = {vnic}
        self.agent._notify_plugged_vnics()
        self.agent._sync_link_states()
        self.agent.eswitch.set_link.assert_called_once_with(
            self.PCI_SLOT, None)
        self.assertEqual({self.PCI_SLOT: (self.MAC, False)},
                         self.agent.link_states)
        self.agent.eswitch.port_up.assert_not_called()
        self.agent.eswitch.set_port_vlan_id.assert_not_called()

    def test_port_with_same_mac_bound_here(self):
        # mlnx_infiniband doesn't bind two ports with the same mac address,
        # but a port of another mechanism driver can have it.
        other = self._port(port_id='port-2', host='host1', migrating_to=None)
        self.ports([other, self._port()])
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')

    def test_unreadable_guid_keeps_handled_plug(self):
        self.agent._notify_plugged_vnics()
        # eswitchd couldn't read the VF's GUID this time.
        vnics = self.agent.eswitch.get_plugged_vnics.return_value
        vnics[self.PCI_SLOT]['guid_checked'] = False
        self.agent._notify_plugged_vnics()
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)
        vnics[self.PCI_SLOT]['guid_checked'] = True
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')

    def test_unreadable_guid_not_reported(self):
        vnics = self.agent.eswitch.get_plugged_vnics.return_value
        vnics[self.PCI_SLOT]['guid_checked'] = False
        self.agent._notify_plugged_vnics()
        self.assert_notified()
        self.rpc.get_ports_by_vnic_type_and_host.assert_not_called()
        vnics[self.PCI_SLOT]['guid_checked'] = True
        self.agent._notify_plugged_vnics()
        self.assert_notified('port-1')

    def _notify_at(self, now):
        with mock.patch.object(mlnx_eswitch_neutron_agent.time, 'monotonic',
                               return_value=now):
            self.agent._notify_plugged_vnics()

    def test_unmatched_plug_given_up(self):
        window = mlnx_eswitch_neutron_agent.PLUGGED_VNIC_MATCH_WINDOW
        self.ports([])
        self._notify_at(1000)
        self._notify_at(1000 + window - 1)
        self.assertEqual({}, self.agent.handled_plugs)
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'info') as info:
            self._notify_at(1000 + window)
        info.assert_called_once()
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)
        self.assertEqual({}, self.agent._unmatched_plugs)
        calls = self.rpc.get_ports_by_vnic_type_and_host.call_count
        # The agent no longer gets the ports for it.
        self._notify_at(1000 + 2 * window)
        self.assertEqual(
            calls, self.rpc.get_ports_by_vnic_type_and_host.call_count)
        self.assert_notified()

    def test_unmatched_plug_window_restarts_on_new_plug(self):
        window = mlnx_eswitch_neutron_agent.PLUGGED_VNIC_MATCH_WINDOW
        self.ports([])
        self._notify_at(1000)
        self.plug('plug-2')
        self._notify_at(1000 + window)
        self.assertEqual({self.PCI_SLOT: ('plug-2', 1000 + window)},
                         self.agent._unmatched_plugs)
        self.assertEqual({}, self.agent.handled_plugs)

    def test_unmatched_plug_forgotten_when_unplugged(self):
        self.ports([])
        self._notify_at(1000)
        self.assertIn(self.PCI_SLOT, self.agent._unmatched_plugs)
        self.agent.eswitch.get_plugged_vnics.return_value = {}
        self._notify_at(1001)
        self.assertEqual({}, self.agent._unmatched_plugs)

    def test_unmatched_plug_matched_later(self):
        self.ports([])
        self._notify_at(1000)
        self.ports([self._port()])
        self._notify_at(1002)
        self.assert_notified('port-1')
        self.assertEqual({}, self.agent._unmatched_plugs)

    def test_ports_failure_logged_once_per_iteration(self):
        vnic = (self.MAC, self.PCI_SLOT)
        self.agent.eswitch.get_vnics.return_value = {vnic}
        self.rpc.get_ports_by_vnic_type_and_host.side_effect = ValueError

        class StopLoop(BaseException):
            pass

        with mock.patch.object(self.agent, 'scan_ports',
                               side_effect=[{'current': {vnic},
                                             'added': set(),
                                             'removed': set(),
                                             'updated': set()},
                                            StopLoop]), \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'sleep'), \
                mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                                  'exception') as log_exception, \
                mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                                  'warning') as log_warning:
            self.assertRaises(StopLoop, self.agent.run)
        log_exception.assert_called_once()
        log_warning.assert_called_once()
        self.assertEqual(
            1, self.rpc.get_ports_by_vnic_type_and_host.call_count)
        self.assert_notified()

    def test_handled_plugs_skipped_by_eswitchd(self):
        self.agent._notify_plugged_vnics()
        self.agent.eswitch.get_plugged_vnics.assert_called_with([])
        # eswitchd doesn't read the GUID of the handled VF again.
        self.plug('plug-1', guid_checked=False)
        self.agent._notify_plugged_vnics()
        self.agent.eswitch.get_plugged_vnics.assert_called_with(['plug-1'])
        self.assertEqual({self.PCI_SLOT: 'plug-1'}, self.agent.handled_plugs)
        self.assert_notified('port-1')

    def test_reply_without_guid_checked_not_reported(self):
        vnics = self.agent.eswitch.get_plugged_vnics.return_value
        del vnics[self.PCI_SLOT]['guid_checked']
        self.agent._notify_plugged_vnics()
        self.assert_notified()

    def test_old_eswitchd_asked_again_after_eswitchd_failure(self):
        # eswitchd restarted, maybe upgraded, which the agent sees as a
        # failed request.
        self.agent.eswitch.get_plugged_vnics.side_effect = (
            exceptions.UnsupportedAction(action='get_plugged_vnics'))
        self._notify_at(1000)
        self.assertIsNotNone(self.agent._plugged_vnics_retry_at)

        class StopLoop(BaseException):
            pass

        with mock.patch.object(self.agent, 'scan_ports',
                               side_effect=[exceptions.OperationFailed(
                                   err_msg='failed'), StopLoop]), \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'sleep'):
            self.assertRaises(StopLoop, self.agent.run)
        self.assertIsNone(self.agent._plugged_vnics_retry_at)
        self.agent.eswitch.get_plugged_vnics.side_effect = None
        self._notify_at(1001)
        self.assert_notified('port-1')

    def test_old_eswitchd_asked_again_after_eswitchd_timeout(self):
        self.agent.eswitch.get_plugged_vnics.side_effect = (
            exceptions.UnsupportedAction(action='get_plugged_vnics'))
        self._notify_at(1000)
        self.assertIsNotNone(self.agent._plugged_vnics_retry_at)

        class StopLoop(BaseException):
            pass

        with mock.patch.object(self.agent, 'scan_ports',
                               side_effect=[exceptions.RequestTimeout(),
                                            StopLoop]), \
                mock.patch.object(mlnx_eswitch_neutron_agent.time,
                                  'sleep'):
            self.assertRaises(StopLoop, self.agent.run)
        self.assertIsNone(self.agent._plugged_vnics_retry_at)

        # The upgraded eswitchd answers.
        self.agent.eswitch.get_plugged_vnics.side_effect = None
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'info') as info:
            self._notify_at(1001)
        info.assert_any_call("eSwitchD now supports get_plugged_vnics")
        self.assertFalse(self.agent._plugged_vnics_unsupported)
        self.assert_notified('port-1')

    def test_old_eswitchd_warned_once_until_supported(self):
        unsupported = exceptions.UnsupportedAction(action='get_plugged_vnics')
        self.agent.eswitch.get_plugged_vnics.side_effect = unsupported
        with mock.patch.object(mlnx_eswitch_neutron_agent.LOG,
                               'warning') as warning:
            self._notify_at(1000)
            # Asked again after eswitchd failed, still old.
            self.agent._plugged_vnics_retry_at = None
            self._notify_at(1001)
            self.assertEqual(1, warning.call_count)
            # A newer eswitchd, then an old one again, each found after a
            # failed request.
            self.agent._plugged_vnics_retry_at = None
            self.agent.eswitch.get_plugged_vnics.side_effect = None
            self._notify_at(1002)
            self.agent.eswitch.get_plugged_vnics.side_effect = unsupported
            self._notify_at(1003)
            self.assertEqual(2, warning.call_count)
