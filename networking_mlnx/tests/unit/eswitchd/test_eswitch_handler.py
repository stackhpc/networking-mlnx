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

from networking_mlnx.eswitchd.common import constants
from networking_mlnx.eswitchd.common import exceptions
from networking_mlnx.eswitchd import eswitch_handler
from networking_mlnx.eswitchd import msg_handler
from networking_mlnx.internal.netdev_ops import constants as netdev_consts
from networking_mlnx.tests import base

PCI_SLOT = '0000:12:00.4'
MAC = 'fa:16:3e:a6:71:17'
OTHER_MAC = 'fa:16:3e:00:00:01'


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

    def test_plug_nic_records_plug(self):
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        plug = self.handler.plugged_vnics[PCI_SLOT]
        self.assertEqual(MAC, plug['mac'])
        self.assertTrue(plug['plug_id'])

    def test_plug_nic_again_gets_new_plug_id(self):
        # For example when Nova retries a live migration to this host.
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        plug_id = self.handler.plugged_vnics[PCI_SLOT]['plug_id']
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.assertNotEqual(plug_id,
                            self.handler.plugged_vnics[PCI_SLOT]['plug_id'])

    def test_plug_nic_failure_forgets_plug(self):
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.mgr.set_device_ib_mac.side_effect = RuntimeError
        self.assertRaises(RuntimeError, self.handler.plug_nic,
                          'physnet', 'device', MAC, PCI_SLOT)
        self.assertEqual({}, self.handler.plugged_vnics)

    def test_delete_port_forgets_plug(self):
        other_slot = '0000:12:00.5'
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.handler.plug_nic('physnet', 'device', OTHER_MAC, other_slot)
        self.mgr.get_device_from_mac.return_value = PCI_SLOT
        self.assertEqual(PCI_SLOT, self.handler.delete_port('physnet', MAC))
        self.mgr.set_device_ib_mac.assert_called_with(PCI_SLOT)
        self.assertEqual([other_slot], list(self.handler.plugged_vnics))

    def test_delete_port_unknown_mac_keeps_plugs(self):
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.mgr.get_device_from_mac.return_value = None
        self.assertIsNone(self.handler.delete_port('physnet', OTHER_MAC))
        self.assertEqual([PCI_SLOT], list(self.handler.plugged_vnics))

    def _macs(self, macs, unreadable=()):
        self.mgr.get_vfs_ib_macs.return_value = (macs, set(unreadable))

    def test_get_plugged_vnics(self):
        other_slot = '0000:12:00.5'
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.handler.plug_nic('physnet', 'device', OTHER_MAC, other_slot)
        plug_id = self.handler.plugged_vnics[PCI_SLOT]['plug_id']
        # The VFs aren't assigned yet, so only get_vfs_ib_macs can read
        # them. Their GUIDs are read together.
        self._macs({PCI_SLOT: MAC.upper(), other_slot: OTHER_MAC})
        vnics = self.handler.get_plugged_vnics()
        self.assertEqual({'mac': MAC, 'pci_slot': PCI_SLOT,
                          'plug_id': plug_id, 'guid_checked': True},
                         vnics[PCI_SLOT])
        self.assertEqual({PCI_SLOT, other_slot}, set(vnics))
        self.mgr.get_vfs_ib_macs.assert_called_once_with(
            [PCI_SLOT, other_slot])

    def test_get_plugged_vnics_forgets_vf_with_other_guid(self):
        for current_mac in (None, '00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff',
                            OTHER_MAC):
            self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
            self._macs({PCI_SLOT: current_mac})
            self.assertEqual({}, self.handler.get_plugged_vnics())
            self.assertEqual({}, self.handler.plugged_vnics)

    def test_get_plugged_vnics_vf_it_cant_read(self):
        other_slot = '0000:12:00.5'
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.handler.plug_nic('physnet', 'device', OTHER_MAC, other_slot)
        self._macs({other_slot: OTHER_MAC}, unreadable=[PCI_SLOT])
        vnics = self.handler.get_plugged_vnics()
        # The VF may still have the GUID, so it is listed and kept.
        self.assertFalse(vnics[PCI_SLOT]['guid_checked'])
        self.assertTrue(vnics[other_slot]['guid_checked'])
        self.assertEqual({PCI_SLOT, other_slot},
                         set(self.handler.plugged_vnics))
        # Once it can be read again.
        self._macs({PCI_SLOT: MAC, other_slot: OTHER_MAC})
        self.assertTrue(
            self.handler.get_plugged_vnics()[PCI_SLOT]['guid_checked'])

    def test_get_plugged_vnics_skips_handled_plugs(self):
        other_slot = '0000:12:00.5'
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        self.handler.plug_nic('physnet', 'device', OTHER_MAC, other_slot)
        plug_id = self.handler.plugged_vnics[PCI_SLOT]['plug_id']
        self._macs({other_slot: OTHER_MAC})
        vnics = self.handler.get_plugged_vnics([plug_id, 'gone'])
        # The skipped VF's GUID isn't read, so it isn't checked either.
        self.mgr.get_vfs_ib_macs.assert_called_once_with([other_slot])
        self.assertEqual({'mac': MAC, 'pci_slot': PCI_SLOT,
                          'plug_id': plug_id, 'guid_checked': False},
                         vnics[PCI_SLOT])
        self.assertTrue(vnics[other_slot]['guid_checked'])

    def test_get_plugged_vnics_all_skipped(self):
        self.handler.plug_nic('physnet', 'device', MAC, PCI_SLOT)
        plug_id = self.handler.plugged_vnics[PCI_SLOT]['plug_id']
        vnics = self.handler.get_plugged_vnics([plug_id])
        self.mgr.get_vfs_ib_macs.assert_not_called()
        self.assertFalse(vnics[PCI_SLOT]['guid_checked'])

    def test_get_plugged_vnics_without_plugs(self):
        self._macs({})
        self.assertEqual({}, self.handler.get_plugged_vnics())

    def test_port_up_with_matching_guid_enables_link(self):
        self.mgr.get_device_ib_mac.return_value = MAC
        self.handler.port_up('physnet', PCI_SLOT, MAC.upper())
        self.mgr.set_device_admin_state.assert_called_once_with(
            PCI_SLOT, netdev_consts.ADMIN_STATE_UP)

    def test_port_up_with_other_guid_keeps_link_down(self):
        for current_mac in (None, '00:00:00:00:00:00', 'fa:16:3e:00:00:01'):
            self.mgr.get_device_ib_mac.return_value = current_mac
            self.assertRaises(exceptions.GuidMismatchException,
                              self.handler.port_up, 'physnet', PCI_SLOT, MAC)
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
                                           RuntimeError('broken'))
        self.assertEqual('FAIL', response['status'])
        self.assertNotIn('error', response)

    def test_port_up_guid_mismatch_is_reported_with_error_code(self):
        msg = {'action': 'port_up', 'fabric': 'physnet',
               'pci_slot': PCI_SLOT, 'mac': MAC}
        with mock.patch.object(msg_handler, 'LOG') as log:
            response, _handler = self._execute(
                msg_handler.PortUp, msg,
                exceptions.GuidMismatchException('wrong GUID'))
        self.assertEqual('FAIL', response['status'])
        self.assertEqual(constants.ERROR_GUID_MISMATCH, response['error'])
        self.assertIn('wrong GUID', response['reason'])
        log.warning.assert_called_once()
        log.error.assert_not_called()

    def test_port_down_failure_is_reported(self):
        msg = {'action': 'port_down', 'fabric': 'physnet',
               'pci_slot': PCI_SLOT}
        response, _handler = self._execute(msg_handler.PortDown, msg,
                                           RuntimeError)
        self.assertEqual('FAIL', response['status'])


class TestPluggedVnicsMessages(base.TestCase):

    def setUp(self):
        super(TestPluggedVnicsMessages, self).setUp()
        self.handler = mock.Mock()
        self.dispatcher = msg_handler.MessageDispatch(self.handler)

    def test_get_plugged_vnics(self):
        vnics = {PCI_SLOT: {'mac': MAC, 'pci_slot': PCI_SLOT,
                            'plug_id': 'plug-id'}}
        self.handler.get_plugged_vnics.return_value = vnics
        response = self.dispatcher.handle_msg(
            {'action': 'get_plugged_vnics'})
        self.assertEqual({'status': 'OK', 'response': vnics,
                          'action': 'get_plugged_vnics'}, response)

    def test_get_plugged_vnics_skip_plug_ids(self):
        self.handler.get_plugged_vnics.return_value = {}
        self.dispatcher.handle_msg({'action': 'get_plugged_vnics',
                                    'skip_plug_ids': ['plug-id']})
        self.handler.get_plugged_vnics.assert_called_once_with(['plug-id'])
        # An older agent doesn't send them.
        self.handler.get_plugged_vnics.reset_mock()
        self.dispatcher.handle_msg({'action': 'get_plugged_vnics'})
        self.handler.get_plugged_vnics.assert_called_once_with(None)

    def test_get_plugged_vnics_failure_is_reported(self):
        self.handler.get_plugged_vnics.side_effect = RuntimeError
        response = self.dispatcher.handle_msg(
            {'action': 'get_plugged_vnics'})
        self.assertEqual('FAIL', response['status'])

    def test_plug_and_delete_port_through_dispatcher(self):
        with mock.patch.object(eswitch_handler.eswitch_manager,
                               'ESwitchManager'), \
                mock.patch.object(eswitch_handler.pci_utils, 'pciUtils'):
            handler = eswitch_handler.eSwitchHandler()
        dispatcher = msg_handler.MessageDispatch(handler)
        mgr = handler.eswitch_mgr
        mgr.get_vfs_ib_macs.side_effect = (
            lambda slots: ({slot: MAC for slot in slots}, set()))
        mgr.get_vfs_ib_macs.reset_mock()
        # Nova's ebrctl add-port, with the mac address in upper case.
        response = dispatcher.handle_msg(
            {'action': 'plug_nic', 'fabric': 'physnet', 'device_id': 'vm',
             'vnic_mac': MAC.upper(), 'dev_name': PCI_SLOT,
             'vnic_type': 'ib_hostdev'})
        self.assertEqual('OK', response['status'])
        response = dispatcher.handle_msg({'action': 'get_plugged_vnics'})
        self.assertEqual({PCI_SLOT}, set(response['response']))
        self.assertEqual(MAC, response['response'][PCI_SLOT]['mac'])
        # Nova's ebrctl del-port.
        mgr.get_device_from_mac.return_value = PCI_SLOT
        response = dispatcher.handle_msg(
            {'action': 'delete_port', 'fabric': 'physnet', 'vnic_mac': MAC})
        self.assertEqual('OK', response['status'])
        response = dispatcher.handle_msg({'action': 'get_plugged_vnics'})
        self.assertEqual({}, response['response'])
