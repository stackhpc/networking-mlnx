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
from unittest import mock

from neutron.db import models_v2
from neutron.db import provisioning_blocks
from neutron.plugins.ml2 import plugin
from neutron_lib.callbacks import resources
from neutron_lib import constants
from neutron_lib.db import api as db_api
import oslo_messaging
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests

from networking_mlnx.db import db
from networking_mlnx.journal import journal
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.tests.unit.journal import test_guid_ordering
from networking_mlnx.tests.unit.ml2.drivers.sdn import test_mechanism_sdn


class TestJournalRpc(test_guid_ordering.SdnJournalTestBase):
    """The worker asks an RPC worker to change ports' status

    The tests' RPC client hands calls straight to SdnJournalRpcCallback.
    """

    def _monitor(self, row, job_id='app/jobs/1'):
        db.update_db_row_job_id(self.context, row, job_id)
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        return db.get_row(self.context, row.id)

    def _sync_job(self, status):
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': status}))
        with mock.patch('requests.request', return_value=response), \
                mock.patch.object(provisioning_blocks,
                                  'provisioning_complete') as complete, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.worker.sync()
        return complete, set_status

    def test_completed_port_post_is_completed_by_rpc_worker(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)

        complete, _set_status = self._sync_job('Completed')

        self.assertEqual(
            [('complete_port_row', {'row_id': row.id, 'job_id': row.job_id,
                                    'retry_count': row.retry_count})],
            self.rpc_client.calls)
        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, row.id).state)
        self.assertFalse(self._is_blocked(port_id))
        complete.assert_called_once()

    def test_completed_network_row_is_completed_locally(self):
        db.create_pending_row(self.context, sdn_const.NETWORK,
                              uuidutils.generate_uuid(), sdn_const.POST, {})
        row = self._monitor(self._rows()[0])

        self._sync_job('Completed')

        self.assertEqual([], self.rpc_client.calls)
        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, row.id).state)

    def test_failed_rpc_call_is_tried_again(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)

        with mock.patch.object(self.rpc_client, 'call',
                               side_effect=oslo_messaging.MessagingTimeout):
            complete, _set_status = self._sync_job('Completed')
        self.assertEqual(sdn_const.MONITORING,
                         db.get_row(self.context, row.id).state)
        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

        complete, _set_status = self._sync_job('Completed')
        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, row.id).state)
        self.assertFalse(self._is_blocked(port_id))

    def test_failed_job_fails_port_by_rpc_worker(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)

        _complete, set_status = self._sync_job('Failed')

        self.assertEqual([('fail_port', {'row_id': row.id})],
                         self.rpc_client.calls)
        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)
        set_status.assert_called_once_with(
            mock.ANY, port_id, constants.PORT_STATUS_ERROR)
        self.assertTrue(self._is_blocked(port_id))

    def test_failed_rpc_call_keeps_failed_port_blocked(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)

        with mock.patch.object(self.rpc_client, 'call',
                               side_effect=oslo_messaging.MessagingTimeout):
            _complete, set_status = self._sync_job('Failed')

        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)
        set_status.assert_not_called()
        self.assertTrue(self._is_blocked(port_id))

    def test_rpc_worker_leaves_changed_row(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        callback = journal.SdnJournalRpcCallback()

        with mock.patch.object(provisioning_blocks,
                               'provisioning_complete') as complete:
            callback.complete_port_row(self.context, row.id, 'app/jobs/0',
                                       row.retry_count)
            callback.complete_port_row(self.context, row.id, row.job_id,
                                       row.retry_count + 1)
            callback.complete_port_row(self.context, 'no-such-row',
                                       row.job_id, row.retry_count)

        self.assertEqual(sdn_const.MONITORING,
                         db.get_row(self.context, row.id).state)
        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

    def test_repeated_rpc_call_completes_row_once(self):
        # As when the reply to the first call is lost.
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        callback = journal.SdnJournalRpcCallback()

        with mock.patch.object(provisioning_blocks,
                               'provisioning_complete') as complete:
            for _ in range(2):
                callback.complete_port_row(self.context, row.id, row.job_id,
                                           row.retry_count)

        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, row.id).state)
        complete.assert_called_once()

    def test_rpc_timeout_skips_later_calls_until_next_sync(self):
        # Without an RPC worker, each call would wait for its timeout.
        first_port_id, first_row = self._make_blocked_port()
        # _make_blocked_port() clears the journal, so record the first
        # port's POST again.
        _port_id, second_row = self._make_blocked_port()
        db.create_pending_row(self.context, sdn_const.PORT, first_port_id,
                              sdn_const.POST,
                              jsonutils.loads(first_row.data))
        rows = [self._monitor(r, 'app/jobs/%s' % i)
                for i, r in enumerate(self._rows())]
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': 'Completed'}))
        with mock.patch.object(db, 'get_all_monitoring_db_row_by_oldest',
                               side_effect=lambda context: rows), \
                mock.patch('requests.request', return_value=response), \
                mock.patch.object(provisioning_blocks,
                                  'provisioning_complete'):
            with mock.patch.object(
                    self.rpc_client, 'call',
                    side_effect=oslo_messaging.MessagingTimeout) as call:
                self.worker.sync()
            call.assert_called_once()
            self.assertEqual(
                [sdn_const.MONITORING] * 2,
                [db.get_row(self.context, r.id).state for r in rows])

            self.worker.sync()
        self.assertEqual(
            [sdn_const.COMPLETED] * 2,
            [db.get_row(self.context, r.id).state for r in rows])

    def test_failing_port_is_skipped_after_rpc_timeout(self):
        # It is asked again at the next sync.
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        self.worker._rpc_timed_out = True

        _complete, set_status = self._sync_job_without_reset('Failed')

        self.assertEqual([], self.rpc_client.calls)
        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)
        self.assertTrue(self._is_blocked(port_id))

    def test_lost_failing_of_port_is_asked_again(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        with mock.patch.object(self.rpc_client, 'call',
                               side_effect=oslo_messaging.MessagingTimeout):
            _complete, set_status = self._sync_job('Failed')
        set_status.assert_not_called()

        _complete, set_status = self._sync_job('Failed')

        self.assertEqual([('fail_port', {'row_id': row.id})],
                         self.rpc_client.calls)
        set_status.assert_called_once_with(
            mock.ANY, port_id, constants.PORT_STATUS_ERROR)
        self.assertTrue(self._is_blocked(port_id))

    def test_port_failed_by_rpc_worker_is_not_failed_again(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        self._sync_job('Failed')
        self.assertEqual([('fail_port', {'row_id': row.id})],
                         self.rpc_client.calls)
        # As update_port_status() does, which the tests mock.
        self._set_port_status(self.context, port_id,
                              constants.PORT_STATUS_ERROR)
        self.rpc_client.calls.clear()

        self._sync_job('Failed')

        self.assertEqual([], self.rpc_client.calls)

    def test_port_no_longer_held_is_not_failed_again(self):
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        with mock.patch.object(self.rpc_client, 'call',
                               side_effect=oslo_messaging.MessagingTimeout):
            self._sync_job('Failed')
        # The port was rebound on another host since.
        self._move_binding(self.context, port_id, 'new-host')

        _complete, set_status = self._sync_job('Failed')

        self.assertEqual([], self.rpc_client.calls)
        set_status.assert_not_called()

    def test_failed_port_posts_not_in_error(self):
        port_id, row = self._make_blocked_port()
        db.update_db_row_state(self.context, row, sdn_const.FAILED)
        db.create_pending_row(self.context, sdn_const.NETWORK,
                              uuidutils.generate_uuid(), sdn_const.POST, {})
        for other in self._rows():
            if other.id != row.id:
                db.update_db_row_state(self.context, other,
                                       sdn_const.FAILED)
        self.assertEqual(
            [row.id],
            [r.id for r in db.get_failed_port_posts_not_in_error(
                self.context, 300)])

        self._set_port_status(self.context, port_id,
                              constants.PORT_STATUS_ERROR)
        self.assertEqual(
            [], db.get_failed_port_posts_not_in_error(self.context, 300))

        self._set_port_status(self.context, port_id,
                              constants.PORT_STATUS_DOWN)
        # Another entity's block doesn't count, nor return the row twice.
        provisioning_blocks.add_provisioning_component(
            self.context, port_id, resources.PORT,
            provisioning_blocks.L2_AGENT_ENTITY)
        self.assertEqual(
            [row.id],
            [r.id for r in db.get_failed_port_posts_not_in_error(
                self.context, 300)])
        provisioning_blocks.remove_provisioning_component(
            self.context, port_id, resources.PORT,
            sdn_const.PROVISIONING_ENTITY)
        self.assertEqual(
            [], db.get_failed_port_posts_not_in_error(self.context, 300))

    def _fail_with_lost_call(self):
        """Fail a held port's POST, with its RPC call lost"""
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        with mock.patch.object(self.rpc_client, 'call',
                               side_effect=oslo_messaging.MessagingTimeout):
            self._sync_job('Failed')
        return port_id, db.get_row(self.context, row.id)

    def test_port_with_newer_post_is_not_failed_again(self):
        # The port is held for its newer POST, which may yet succeed.
        port_id, row = self._fail_with_lost_call()
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST, jsonutils.loads(row.data))
        response = self._response(requests.codes.service_unavailable)
        with mock.patch('requests.request', return_value=response), \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.worker._fail_ports_again(self.context)

        self.assertEqual([], self.rpc_client.calls)
        set_status.assert_not_called()

    def test_ports_are_not_failed_again_after_window(self):
        port_id, row = self._fail_with_lost_call()
        self._age(self.context, row, journal.FAIL_AGAIN_WINDOW + 60)

        _complete, set_status = self._sync_job('Failed')

        self.assertEqual([], self.rpc_client.calls)
        set_status.assert_not_called()

    def test_failing_again_stops_at_rpc_timeout(self):
        first_port_id, first_row = self._fail_with_lost_call()
        # _make_blocked_port() clears the journal, so record the first
        # port's failed POST again.
        second_port_id, second_row = self._fail_with_lost_call()
        db.create_pending_row(self.context, sdn_const.PORT, first_port_id,
                              sdn_const.POST,
                              jsonutils.loads(first_row.data))
        for row in self._rows():
            db.update_db_row_state(self.context, row, sdn_const.FAILED)
        self.assertEqual(
            2, len(db.get_failed_port_posts_not_in_error(self.context,
                                                         300)))

        with mock.patch.object(
                self.rpc_client, 'call',
                side_effect=oslo_messaging.MessagingTimeout) as call, \
                mock.patch.object(journal, '_get_blocked_port_id',
                                  wraps=journal._get_blocked_port_id
                                  ) as get_blocked:
            self.worker.sync()

        call.assert_called_once()
        # The second row isn't looked at.
        get_blocked.assert_called_once()

    def test_two_failed_posts_of_a_port(self):
        # Only the newest holds the port.
        port_id, row = self._make_blocked_port()
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST, jsonutils.loads(row.data))
        for each in self._rows():
            db.update_db_row_state(self.context, each, sdn_const.FAILED)
        rows = db.get_failed_port_posts_not_in_error(self.context, 300)
        self.assertEqual(2, len(rows))
        self.assertEqual([row.id, mock.ANY], [r.id for r in rows])

        with mock.patch.object(plugin.Ml2Plugin, 'update_port_status'):
            self.worker._fail_ports_again(self.context)

        self.assertEqual([('fail_port', {'row_id': rows[1].id})],
                         self.rpc_client.calls)

    @db_api.CONTEXT_WRITER
    def _age(self, context, row, seconds):
        row.last_retried -= datetime.timedelta(seconds=seconds)
        context.session.merge(row)

    @db_api.CONTEXT_WRITER
    def _set_port_status(self, context, port_id, status):
        context.session.query(models_v2.Port).filter_by(
            id=port_id).update({'status': status})

    _move_binding = (
        test_mechanism_sdn.SdnPkeyProvisioningTestCase._move_binding)

    def _sync_job_without_reset(self, status):
        """Check the jobs as the rest of a sync in which a call timed out"""
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': status}))
        with mock.patch('requests.request', return_value=response), \
                mock.patch.object(provisioning_blocks,
                                  'provisioning_complete') as complete, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.worker._sync_progress_rows(self.context)
        return complete, set_status

    def test_rejected_post_survives_rpc_timeout(self):
        # The POST is failed from the pending rows, not from its job.
        port_id, row = self._make_blocked_port()
        response = self._response(requests.codes.not_implemented)
        with mock.patch('requests.request', return_value=response), \
                mock.patch.object(self.rpc_client, 'call',
                                  side_effect=oslo_messaging.MessagingTimeout
                                  ) as call, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.worker._sync_pending_rows(self.context, False)

        call.assert_called_once_with(mock.ANY, 'fail_port', row_id=row.id)
        set_status.assert_not_called()
        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)
        self.assertTrue(self._is_blocked(port_id))

    def test_rpc_worker_fails_only_failed_rows(self):
        # As when a late call arrives after the row was sent again.
        port_id, row = self._make_blocked_port()
        row = self._monitor(row)
        callback = journal.SdnJournalRpcCallback()

        with mock.patch.object(plugin.Ml2Plugin,
                               'update_port_status') as set_status:
            callback.fail_port(self.context, row.id)
            callback.fail_port(self.context, 'no-such-row')

        set_status.assert_not_called()
        self.assertTrue(self._is_blocked(port_id))


class TestJournalRpcApi(test_guid_ordering.SdnJournalTestBase):

    def test_calls(self):
        port_id, row = self._make_blocked_port()
        with mock.patch.object(journal.n_rpc, 'get_client') as get_client:
            api = journal.SdnJournalRpcApi()
            api.complete_port_row(self.context, row)
            api.fail_port(self.context, row)

        target = get_client.call_args.args[0]
        self.assertEqual(journal.RPC_TOPIC, target.topic)
        self.assertEqual('1.0', target.version)
        # An explicit timeout, which neutron-lib doesn't back off.
        self.assertEqual(
            [mock.call(timeout=journal.RPC_TIMEOUT)] * 2,
            get_client.return_value.prepare.call_args_list)
        cctxt = get_client.return_value.prepare.return_value
        self.assertEqual(
            [mock.call(self.context, 'complete_port_row', row_id=row.id,
                       job_id=row.job_id, retry_count=row.retry_count),
             mock.call(self.context, 'fail_port', row_id=row.id)],
            cctxt.call.call_args_list)
