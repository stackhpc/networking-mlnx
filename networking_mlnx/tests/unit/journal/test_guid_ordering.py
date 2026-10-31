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

from neutron.plugins.ml2 import plugin
from neutron_lib import context as nl_context
from oslo_serialization import jsonutils
from oslo_utils import uuidutils

from networking_mlnx.db import db
from networking_mlnx.journal import dependency_validations
from networking_mlnx.journal import journal
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import sdn_mech_driver
from networking_mlnx.tests import base
from networking_mlnx.tests.unit.ml2.drivers.sdn import test_mechanism_sdn

MAC = 'fa:16:3e:04:ab:19'
CLIENT_ID = test_mechanism_sdn.CLIENT_ID
_Pkey = test_mechanism_sdn.SdnPkeyProvisioningTestCase


class SdnJournalTestBase(test_mechanism_sdn.SdnConfigBase):
    """Borrows the pkey tests' helpers, but none of their tests"""

    HOST = _Pkey.HOST
    _db_cleanup = _Pkey._db_cleanup
    _rows = _Pkey._rows
    _make_blocked_port = _Pkey._make_blocked_port
    _is_blocked = _Pkey._is_blocked
    _response = staticmethod(test_mechanism_sdn.SdnDriverTestCase._response)

    def setUp(self):
        super(SdnJournalTestBase, self).setUp()
        self.context = nl_context.get_admin_context()
        mock.patch.object(journal.SdnJournalThread,
                          'start_sync_thread').start()
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self.thread = journal.SdnJournalThread()
        self.addCleanup(self._db_cleanup)


class TestGuidIdentity(base.TestCase):
    """get_port_guid_identity() needs no database"""

    def test_vf_port(self):
        self.assertEqual(MAC, db.get_port_guid_identity(
            {'binding:vnic_type': 'direct', 'mac_address': MAC.upper()}))

    def test_baremetal_port(self):
        for opt_name in ('client-id', '61', 61):
            self.assertEqual(CLIENT_ID, db.get_port_guid_identity(
                {'binding:vnic_type': 'baremetal', 'mac_address': MAC,
                 'extra_dhcp_opts': [{'opt_name': opt_name,
                                      'opt_value': CLIENT_ID.upper()}]}))

    def test_baremetal_port_without_client_id(self):
        self.assertEqual(MAC, db.get_port_guid_identity(
            {'binding:vnic_type': 'baremetal', 'mac_address': MAC}))


class TestGuidOrdering(SdnJournalTestBase):
    """A port's POST waits for other ports with its GUID to be deleted"""

    def _port_data(self, mac=MAC, client_id=None, port_id=None):
        data = {'id': port_id or uuidutils.generate_uuid(),
                'network_id': uuidutils.generate_uuid(),
                'mac_address': mac,
                'binding:host_id': self.HOST,
                'binding:vnic_type': 'direct'}
        if client_id:
            data['binding:vnic_type'] = 'baremetal'
            data['extra_dhcp_opts'] = [{'opt_name': 'client-id',
                                        'opt_value': client_id}]
        return data

    def _record(self, operation, data, state=None):
        db.create_pending_row(self.context, sdn_const.PORT, data['id'],
                              operation, data)
        row = [r for r in self._rows()
               if r.object_uuid == data['id'] and r.operation == operation][0]
        if state:
            db.update_db_row_state(self.context, row, state)
        return row

    def _post_waits(self, post_data, delete_data,
                    delete_state=sdn_const.MONITORING):
        self._record(sdn_const.DELETE, delete_data, delete_state)
        post = self._record(sdn_const.POST, post_data)
        return not dependency_validations.validate(self.context, post)

    def test_baremetal_post_waits_for_provisioning_port_delete(self):
        # Ironic deletes the provisioning port and binds the tenant port,
        # which has the same client ID.
        for state in (sdn_const.PENDING, sdn_const.PROCESSING,
                      sdn_const.MONITORING):
            self.assertTrue(self._post_waits(
                self._port_data(mac='fa:16:3e:00:00:01', client_id=CLIENT_ID),
                self._port_data(mac='fa:16:3e:00:00:02', client_id=CLIENT_ID),
                state))
            self._db_cleanup()

    def test_vf_post_waits_for_delete_of_port_with_same_mac(self):
        self.assertTrue(self._post_waits(self._port_data(),
                                         self._port_data()))

    def test_post_does_not_wait_for_other_guid(self):
        self.assertFalse(self._post_waits(
            self._port_data(), self._port_data(mac='fa:16:3e:00:00:99')))
        self._db_cleanup()
        self.assertFalse(self._post_waits(
            self._port_data(client_id=CLIENT_ID),
            self._port_data(client_id=CLIENT_ID[:-2] + '99')))

    def test_post_does_not_wait_for_finished_delete(self):
        for state in (sdn_const.COMPLETED, sdn_const.FAILED):
            self.assertFalse(self._post_waits(
                self._port_data(), self._port_data(), state))
            self._db_cleanup()

    def test_post_fails_after_failed_delete_of_same_guid(self):
        port_id, row = self._make_blocked_port()
        data = jsonutils.loads(row.data)
        data['mac_address'] = MAC
        db.delete_row(self.context, row=row)
        self._record(sdn_const.POST, data)
        self._record(sdn_const.DELETE, self._port_data(), sdn_const.FAILED)

        with mock.patch('requests.request') as request, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.thread._sync_pending_rows(self.context, False)

        request.assert_not_called()
        post = [r for r in self._rows() if r.operation == sdn_const.POST][0]
        self.assertEqual(sdn_const.FAILED, post.state)
        set_status.assert_called_once_with(mock.ANY, port_id, 'ERROR')
        self.assertTrue(self._is_blocked(port_id))
