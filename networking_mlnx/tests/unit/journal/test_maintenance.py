# Copyright 2016 Mellanox Technologies, Ltd
# All Rights Reserved.
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

import threading
import unittest.mock as mock

from neutron.tests.unit import testlib_api
from neutron_lib import context
from neutron_lib.db import api as db_api

from oslo_config import fixture

from networking_mlnx.db.models import sdn_maintenance_db
from networking_mlnx.journal import maintenance

from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import sdn_mech_driver


class MaintenanceThreadTestCase(testlib_api.SqlTestCaseLight):
    def setUp(self):
        super(MaintenanceThreadTestCase, self).setUp()
        # 1. Setup the Database for functional testing
        self.conf = self.useFixture(fixture.Config()).conf
        # This helper sets up a clean SQLite/MySQL DB for the test
        self.db_context = context.get_admin_context()

        # 2. Inject the required value
        # You can use a dummy URL for functional tests that mock the backend
        self.conf.set_override('url', 'http://127.0.0.1:8080', group='sdn')

        # 2. Initialize the Driver
        self.driver = sdn_mech_driver.SDNMechanismDriver()
        self.driver.initialize()

        self.add_sdn_maintenance_row_with_pending_state()

        # 3. Prevent the worker from starting a background thread automatically
        # We want to trigger it manually to keep the test deterministic
        mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall').start()

    def add_sdn_maintenance_row_with_pending_state(self):
        with db_api.CONTEXT_WRITER.using(self.db_context):
            row = sdn_maintenance_db.SdnMaintenance(state=sdn_const.PENDING)
            self.db_context.session.add(row)

    def tearDown(self):
        # 4. CRITICAL: Stop the worker before the DB schema is deleted
        if hasattr(self.driver, 'maintenance_worker'):
            self.driver.maintenance_worker.stop()
            # Ensure the worker thread actually finishes
            self.driver.maintenance_worker.wait()
        super(MaintenanceThreadTestCase, self).tearDown()

    def test__execute_op_no_exception(self):
        with mock.patch.object(maintenance, 'LOG') as mock_log:
            operation = mock.MagicMock()
            operation.__name__ = "test"
            self.driver.maintenance_worker._execute_op(
                operation, self.db_context)
            self.assertTrue(operation.called)
            self.assertTrue(mock_log.info.called)
            self.assertFalse(mock_log.exception.called)

    def test_multiple_iterations(self):
        # NOTE(wszumski): This used to test using a real worker thread, but
        # we lost this functionality when moving to neutron workers.
        # The plan is to Restore it in a functional test. For now just
        # simulate with a few iterations.
        callback_event = threading.Event()
        count = [0]

        def callback_op(**kwargs):
            count[0] += 1

            # The following should be true on the second call, so we're making
            # sure that the thread runs more than once.
            if count[0] > 1:
                callback_event.set()

        self.driver.maintenance_worker.register_operation(callback_op)
        for _ in range(0, 2):
            self.driver.maintenance_worker.run()

        # Make sure the callback event was called and not timed out
        self.assertTrue(callback_event.wait(timeout=5))

    def test_thread_continues_after_exception(self):
        exception_event = threading.Event()
        callback_event = threading.Event()

        def exception_op(**kwargs):
            if not exception_event.is_set():
                exception_event.set()
                raise Exception()

        def callback_op(**kwargs):
            callback_event.set()

        for op in [exception_op, callback_op]:
            self.driver.maintenance_worker.register_operation(op)

        self.driver.maintenance_worker.run()

        # Make sure the callback event was called and not timed out
        self.assertTrue(callback_event.wait(timeout=5))
