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

import datetime
import unittest.mock as mock

from neutron.db import provisioning_blocks
from neutron.plugins.ml2 import plugin
from neutron_lib.callbacks import resources
from neutron_lib import context as nl_context
from neutron_lib.db import api as db_api
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests

from networking_mlnx.db import db
from networking_mlnx.journal import cleanup
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


class TestGuidOrderingByAge(SdnJournalTestBase):
    """Only older deletions are waited for, so rows can't wait in a cycle"""

    _port_data = TestGuidOrdering._port_data
    _record = TestGuidOrdering._record

    @db_api.CONTEXT_WRITER
    def _set_created_at(self, context, row, created_at):
        row.created_at = created_at
        context.session.merge(row)

    def _at(self, row, minutes):
        self._set_created_at(
            self.context, row,
            datetime.datetime(2026, 10, 31, 12, 0) +
            datetime.timedelta(minutes=minutes))

    def test_post_does_not_wait_for_newer_delete(self):
        post = self._record(sdn_const.POST, self._port_data())
        delete = self._record(sdn_const.DELETE, self._port_data(),
                              sdn_const.MONITORING)
        self._at(post, 0)
        self._at(delete, 1)
        self.assertTrue(dependency_validations.validate(self.context, post))

    def test_two_deploy_attempts_dont_wait_for_each_other(self):
        # A controller outage spans two deployment attempts, each of which
        # binds a port and then deletes it.
        port_1 = self._port_data(client_id=CLIENT_ID)
        port_2 = self._port_data(client_id=CLIENT_ID)
        rows = [self._record(sdn_const.POST, port_1),
                self._record(sdn_const.DELETE, port_1),
                self._record(sdn_const.POST, port_2),
                self._record(sdn_const.DELETE, port_2)]
        for minutes, row in enumerate(rows):
            self._at(row, minutes)
        self.assertTrue(dependency_validations.validate(self.context,
                                                        rows[0]))


class TestKeepFailedPortDeletes(SdnJournalTestBase):

    @db_api.CONTEXT_WRITER
    def _make_old(self, context, row):
        row.last_retried -= datetime.timedelta(days=1)
        context.session.merge(row)

    def test_failed_port_delete_is_kept(self):
        rows = {}
        for object_type, operation in ((sdn_const.PORT, sdn_const.DELETE),
                                       (sdn_const.PORT, sdn_const.POST),
                                       (sdn_const.NETWORK, sdn_const.DELETE)):
            db.create_pending_row(self.context, object_type,
                                  uuidutils.generate_uuid(), operation, {})
        for row in self._rows():
            db.update_db_row_state(self.context, row, sdn_const.FAILED)
            self._make_old(self.context, row)
            rows[(row.object_type, row.operation)] = row
        cleanup.JournalCleanup().delete_completed_rows(self.context)
        self.assertEqual([(sdn_const.PORT, sdn_const.DELETE)],
                         [(r.object_type, r.operation) for r in self._rows()])


class TestReleaseBeforeCompleting(SdnJournalTestBase):
    """A row is only COMPLETED once its port has been released"""

    def _sync_job(self, row):
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': 'Completed'}))
        with mock.patch('requests.request', return_value=response):
            self.thread._sync_progress_rows(self.context)

    def test_failed_release_is_retried(self):
        port_id, row = self._make_blocked_port()
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)

        with mock.patch.object(provisioning_blocks,
                               'remove_provisioning_component',
                               side_effect=RuntimeError):
            self._sync_job(row)
        self.assertEqual(sdn_const.MONITORING, self._rows()[0].state)
        self.assertTrue(self._is_blocked(port_id))

        self._sync_job(row)
        self.assertEqual(sdn_const.COMPLETED, self._rows()[0].state)
        self.assertFalse(self._is_blocked(port_id))

    def test_row_without_port_is_completed(self):
        port_id, row = self._make_blocked_port()
        provisioning_blocks.remove_provisioning_component(
            self.context, port_id, resources.PORT,
            sdn_const.PROVISIONING_ENTITY)
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        self._sync_job(row)
        self.assertEqual(sdn_const.COMPLETED, self._rows()[0].state)
