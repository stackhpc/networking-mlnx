# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime
import unittest.mock as mock

import netaddr
from neutron.objects import network as network_obj
from neutron.objects import ports as ports_obj
from neutron.tests.unit import testlib_api
from neutron_lib import constants as n_const
from neutron_lib import context as nl_context
from neutron_lib.db import api as db_api
from oslo_config import cfg
from oslo_utils import uuidutils

from networking_mlnx.db import db
from networking_mlnx.db.models import sdn_journal_db
from networking_mlnx.journal import cleanup
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_exc

MAC = 'fa:16:3e:00:00:01'


class JournalRecoveryTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.context = nl_context.get_admin_context()
        self.network_id = uuidutils.generate_uuid()
        network_obj.Network(self.context, id=self.network_id,
                            name='ib').create()

    def _failed_row(self, operation=sdn_const.DELETE,
                    object_type=sdn_const.PORT, object_uuid=None,
                    age=7200, data=None):
        object_uuid = object_uuid or uuidutils.generate_uuid()
        if data is None:
            data = {'id': object_uuid, 'network_id': self.network_id,
                    'mac_address': MAC, 'binding:vnic_type': 'direct'}
        db.create_pending_row(self.context, object_type, object_uuid,
                              operation, data)
        row = db.get_rows(self.context, object_uuid=object_uuid)[-1]
        db.update_db_row_state(self.context, row, sdn_const.FAILED)
        self._set_age(row.id, age)
        return row.id

    @db_api.CONTEXT_WRITER
    def _set_age_in_db(self, context, row_id, age):
        context.session.query(sdn_journal_db.SdnJournal).filter_by(
            id=row_id).update({'last_retried': datetime.datetime.utcnow() -
                               datetime.timedelta(seconds=age)})

    def _set_age(self, row_id, age):
        self._set_age_in_db(self.context, row_id, age)

    def _bound_port(self):
        port = ports_obj.Port(
            self.context, id=uuidutils.generate_uuid(),
            network_id=self.network_id,
            mac_address=netaddr.EUI(MAC, dialect=netaddr.mac_unix_expanded),
            device_id='', device_owner='', admin_state_up=True,
            status=n_const.PORT_STATUS_ACTIVE)
        port.create()
        ports_obj.PortBinding(self.context, port_id=port.id, host='host',
                              vif_type='ib_hostdev', vnic_type='direct',
                              status=n_const.ACTIVE).create()
        return port.id

    def _recover(self, interval=3600):
        cfg.CONF.set_override('failed_port_delete_retry_interval', interval,
                              sdn_const.GROUP_OPT)
        self.addCleanup(cfg.CONF.clear_override,
                        'failed_port_delete_retry_interval',
                        sdn_const.GROUP_OPT)
        with mock.patch.object(cleanup, 'LOG') as log:
            cleanup.JournalRecovery().retry_failed_port_deletes(self.context)
        return log

    def _state(self, row_id):
        return db.get_row(self.context, row_id).state

    def test_retries_failed_port_delete_after_interval(self):
        row_id = self._failed_row()
        log = self._recover()
        self.assertEqual(sdn_const.PENDING, self._state(row_id))
        self.assertEqual(0, db.get_row(self.context, row_id).retry_count)
        log.info.assert_called_once()

    def test_waits_for_interval(self):
        row_id = self._failed_row(age=60)
        self._recover()
        self.assertEqual(sdn_const.FAILED, self._state(row_id))

    def test_disabled(self):
        row_id = self._failed_row()
        self._recover(interval=-1)
        self.assertEqual(sdn_const.FAILED, self._state(row_id))

    def test_ignores_other_failed_rows(self):
        rows = [self._failed_row(operation=sdn_const.POST),
                self._failed_row(object_type=sdn_const.NETWORK)]
        self._recover()
        for row_id in rows:
            self.assertEqual(sdn_const.FAILED, self._state(row_id))

    def test_warns_once_per_interval_when_refused(self):
        # Sending it could remove the GUID that the bound port uses.
        self._bound_port()
        row_id = self._failed_row()
        log = self._recover()
        self.assertEqual(sdn_const.FAILED, self._state(row_id))
        log.warning.assert_called_once()
        self.assertIn('neutron-mlnx-manage journal delete',
                      log.warning.call_args[0][0])

        log = self._recover()
        log.warning.assert_not_called()

        self._set_age(row_id, 7200)
        log = self._recover()
        log.warning.assert_called_once()

    def test_refuses_superseded_row(self):
        row_id = self._failed_row()
        row = db.get_row(self.context, row_id)
        db.create_pending_row(self.context, sdn_const.PORT, row.object_uuid,
                              sdn_const.POST, {'id': row.object_uuid})
        log = self._recover()
        self.assertEqual(sdn_const.FAILED, self._state(row_id))
        log.warning.assert_called_once()

    def test_refuses_row_without_guid_or_network(self):
        # Rows from earlier releases may not record them.
        rows = [self._failed_row(data={'id': 'a', 'mac_address': MAC}),
                self._failed_row(data={'id': 'b',
                                       'network_id': self.network_id})]
        log = self._recover()
        for row_id in rows:
            self.assertEqual(sdn_const.FAILED, self._state(row_id))
        self.assertEqual(2, log.warning.call_count)
        self.assertIn("doesn't record the port's GUID and network",
                      str(log.warning.call_args))

    def test_ignores_row_dealt_with_meanwhile(self):
        row_id = self._failed_row()
        with mock.patch.object(db, 'retry_failed_row',
                               side_effect=sdn_exc.JournalRowNotFailed(
                                   row_id=row_id, state='pending')):
            log = self._recover()
        log.warning.assert_not_called()
        log.exception.assert_not_called()

    def test_error_on_one_row_does_not_stop_others(self):
        bad = self._failed_row(age=9000)
        good = self._failed_row()
        retry = db.retry_failed_row

        def retry_failed_row(context, row_id):
            if row_id == bad:
                raise RuntimeError('boom')
            return retry(context, row_id)

        with mock.patch.object(db, 'retry_failed_row',
                               side_effect=retry_failed_row):
            log = self._recover()
        log.exception.assert_called_once()
        self.assertEqual(sdn_const.PENDING, self._state(good))
        # Tried again only after another interval.
        self.assertEqual(sdn_const.FAILED, self._state(bad))
        self.assertEqual([], [row.id for row in db.get_failed_port_deletes(
            self.context, datetime.timedelta(seconds=3600))])

    def test_operation_has_no_docstring(self):
        # MaintenanceWorker._execute_op reads func_doc, which Python 3
        # functions don't have, if an operation has a docstring.
        self.assertIsNone(
            cleanup.JournalRecovery().retry_failed_port_deletes.__doc__)
