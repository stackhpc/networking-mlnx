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

from unittest import mock

from neutron.db import provisioning_blocks
from neutron.plugins.ml2 import plugin
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests

from networking_mlnx.db import db
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.tests.unit.journal import test_guid_ordering


class TestConcurrentJournalThreads(test_guid_ordering.SdnJournalTestBase):
    """Journal threads that read a MONITORING row before another changed it

    Every journal thread checks the jobs of all MONITORING rows, so one may
    act on a row that another thread has already completed, retried or
    failed, and that may have been sent again since.
    """

    def _monitor(self, row, job_id):
        db.update_db_row_job_id(self.context, row, job_id)
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        return db.get_row(self.context, row.id)

    def _sync_job(self, rows, status=None, text=None):
        """Check the jobs of rows, as a thread that read them earlier

        The controller answers with the job's status, or with text.
        """
        if text is None:
            text = jsonutils.dumps({'Status': status})
        response = self._response(requests.codes.ok, text)
        with mock.patch.object(db, 'get_all_monitoring_db_row_by_oldest',
                               return_value=rows), \
                mock.patch('requests.request', return_value=response), \
                mock.patch.object(provisioning_blocks,
                                  'provisioning_complete') as complete, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.thread._sync_progress_rows(self.context)
        return complete, set_status

    def _resend(self, row, job_id):
        """Retry a row's failed job, and send it again, as another thread"""
        db.update_pending_db_row_retry(self.context,
                                       db.get_row(self.context, row.id), -1)
        return self._monitor(db.get_row(self.context, row.id), job_id)

    def test_failed_job_of_row_sent_again_is_ignored(self):
        db.create_pending_row(self.context, sdn_const.NETWORK,
                              uuidutils.generate_uuid(), sdn_const.POST, {})
        stale = self._monitor(self._rows()[0], 'app/jobs/1')
        self._resend(stale, 'app/jobs/2')

        self._sync_job([stale], 'Failed')

        row = db.get_row(self.context, stale.id)
        self.assertEqual(sdn_const.MONITORING, row.state)
        self.assertEqual('app/jobs/2', row.job_id)
        self.assertEqual(1, row.retry_count)

    def test_failed_job_of_row_sent_again_does_not_fail_port(self):
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')
        self._resend(stale, 'app/jobs/2')

        _complete, set_status = self._sync_job([stale], 'Failed')

        self.assertEqual(sdn_const.MONITORING,
                         db.get_row(self.context, stale.id).state)
        set_status.assert_not_called()
        self.assertTrue(self._is_blocked(port_id))

    def test_completed_job_of_row_sent_again_does_not_complete_it(self):
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')
        self._resend(stale, 'app/jobs/2')

        complete, _set_status = self._sync_job([stale], 'Completed')

        self.assertEqual(sdn_const.MONITORING,
                         db.get_row(self.context, stale.id).state)
        complete.assert_not_called()
        self.assertTrue(self._is_blocked(port_id))

    def test_completed_row_is_completed_once(self):
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')

        first, _set_status = self._sync_job([stale], 'Completed')
        with mock.patch.object(db, 'update_db_row_state') as set_state:
            second, _set_status = self._sync_job([stale], 'Completed')

        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, stale.id).state)
        self.assertFalse(self._is_blocked(port_id))
        first.assert_called_once()
        second.assert_not_called()
        set_state.assert_not_called()

    def test_unreadable_job_of_completed_row_is_ignored(self):
        # The row isn't sent again.
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')
        self._sync_job([stale], 'Completed')

        self._sync_job([stale], text='<html>Bad Gateway</html>')

        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, stale.id).state)

    def test_retried_row_is_retried_once(self):
        db.create_pending_row(self.context, sdn_const.NETWORK,
                              uuidutils.generate_uuid(), sdn_const.POST, {})
        stale = self._monitor(self._rows()[0], 'app/jobs/1')

        self._sync_job([stale], 'Failed')
        self._sync_job([stale], 'Failed')

        row = db.get_row(self.context, stale.id)
        self.assertEqual(sdn_const.PENDING, row.state)
        self.assertEqual(1, row.retry_count)

    def test_port_of_row_out_of_retries_is_failed_once(self):
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')
        self.thread._row_retry_count = 0

        _complete, first = self._sync_job([stale], text='not JSON')
        _complete, second = self._sync_job([stale], text='not JSON')

        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, stale.id).state)
        first.assert_called_once()
        second.assert_not_called()

    def test_port_binding_is_locked_before_monitoring_row(self):
        # The order in which ML2 locks them when it records a row.
        port_id, row = self._make_blocked_port()
        stale = self._monitor(row, 'app/jobs/1')
        calls = mock.Mock()
        with mock.patch.object(db, 'lock_monitoring_row',
                               wraps=db.lock_monitoring_row) as lock_row, \
                mock.patch.object(db, 'lock_port_binding',
                                  wraps=db.lock_port_binding) as lock_port:
            calls.attach_mock(lock_row, 'lock_row')
            calls.attach_mock(lock_port, 'lock_port')
            self._sync_job([stale], 'Completed')

        self.assertEqual(['lock_port', 'lock_row'],
                         [c[0] for c in calls.mock_calls])
        self.assertFalse(self._is_blocked(port_id))
