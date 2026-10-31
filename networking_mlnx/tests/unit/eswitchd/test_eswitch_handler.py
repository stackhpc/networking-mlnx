# Copyright 2026 StackHPC Ltd.
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

from networking_mlnx.eswitchd import eswitch_handler
from networking_mlnx.eswitchd import msg_handler
from networking_mlnx.internal.netdev_ops import constants as netdev_consts
from networking_mlnx.tests import base

PCI_SLOT = '0000:12:00.4'
MAC = 'fa:16:3e:a6:71:17'


class TestESwitchHandler(base.TestCase):

    def setUp(self):
        super(TestESwitchHandler, self).setUp()
        with mock.patch.object(eswitch_handler.eswitch_manager,
                               'ESwitchManager'), \
                mock.patch.object(eswitch_handler.pci_utils, 'pciUtils'):
            self.handler = eswitch_handler.eSwitchHandler()
        self.mgr = self.handler.eswitch_mgr

    def test_plug_nic_with_new_guid_disables_link(self):
        self.mgr.get_device_ib_mac.return_value = None
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)

        self.assertEqual(
            [mock.call.set_device_admin_state(
                PCI_SLOT, netdev_consts.ADMIN_STATE_DOWN),
             mock.call.set_device_ib_mac(PCI_SLOT, MAC),
             mock.call.set_device_admin_state(
                PCI_SLOT, netdev_consts.ADMIN_STATE_DOWN)],
            [c for c in self.mgr.mock_calls
             if c[0] != 'get_device_ib_mac'])

    def test_plug_nic_with_other_ports_guid_disables_link(self):
        self.mgr.get_device_ib_mac.return_value = 'fa:16:3e:00:00:01'
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.mgr.set_device_admin_state.assert_called_with(
            PCI_SLOT, netdev_consts.ADMIN_STATE_DOWN)

    def test_plug_nic_with_same_guid_disables_link(self):
        # The same MAC address can belong to a port on another network. The
        # agent enables the link again if the port is ready.
        self.mgr.get_device_ib_mac.return_value = MAC.upper()
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)

        self.mgr.set_device_ib_mac.assert_called_once_with(PCI_SLOT, MAC)
        self.mgr.set_device_admin_state.assert_called_with(
            PCI_SLOT, netdev_consts.ADMIN_STATE_DOWN)

    def test_plug_nic_fails_if_link_cant_be_disabled(self):
        self.mgr.get_device_ib_mac.return_value = None
        self.mgr.set_device_admin_state.side_effect = RuntimeError
        self.assertRaises(RuntimeError, self.handler.plug_nic,
                          'physnet', 'device', MAC, PCI_SLOT)
        self.mgr.set_device_ib_mac.assert_not_called()

    def test_port_up_with_matching_guid_enables_link(self):
        self.mgr.get_device_ib_mac.return_value = MAC
        self.handler.port_up('physnet', PCI_SLOT, MAC.upper())
        self.mgr.set_device_admin_state.assert_called_once_with(
            PCI_SLOT, netdev_consts.ADMIN_STATE_UP)

    def test_port_up_with_other_guid_keeps_link_down(self):
        for current_mac in (None, '00:00:00:00:00:00', 'fa:16:3e:00:00:01'):
            self.mgr.get_device_ib_mac.return_value = current_mac
            self.assertRaises(RuntimeError, self.handler.port_up,
                              'physnet', PCI_SLOT, MAC)
        self.mgr.set_device_admin_state.assert_not_called()

    def test_port_up_without_mac_enables_link(self):
        self.handler.port_up('physnet', PCI_SLOT)
        self.mgr.get_device_ib_mac.assert_not_called()
        self.mgr.set_device_admin_state.assert_called_once_with(
            PCI_SLOT, netdev_consts.ADMIN_STATE_UP)


class TestLinkMessages(base.TestCase):

    def _execute(self, handler_class, msg, side_effect=None):
        handler = mock.Mock()
        getattr(handler, msg['action']).side_effect = side_effect
        return handler_class(msg).execute(handler), handler

    def test_port_up_passes_mac(self):
        msg = {'action': 'port_up', 'fabric': 'physnet',
               'pci_slot': PCI_SLOT, 'mac': MAC}
        response, handler = self._execute(msg_handler.PortUp, msg)
        self.assertEqual('OK', response['status'])
        handler.port_up.assert_called_once_with('physnet', PCI_SLOT, MAC)

    def test_port_up_failure_is_reported(self):
        msg = {'action': 'port_up', 'fabric': 'physnet',
               'pci_slot': PCI_SLOT, 'mac': MAC}
        response, _handler = self._execute(msg_handler.PortUp, msg,
                                           RuntimeError('wrong GUID'))
        self.assertEqual('FAIL', response['status'])

    def test_port_down_failure_is_reported(self):
        msg = {'action': 'port_down', 'fabric': 'physnet',
               'pci_slot': PCI_SLOT}
        response, _handler = self._execute(msg_handler.PortDown, msg,
                                           RuntimeError)
        self.assertEqual('FAIL', response['status'])
