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
import time

from neutron_lib import context
from oslo_config import cfg
from oslo_log import log as logging

from networking_mlnx.db import db


LOG = logging.getLogger(__name__)


class MaintenanceThread(object):
    def __init__(self):
        self.maintenance_thread = threading.Thread(
            target=self._maintenance_loop)
        self.maintenance_interval = cfg.CONF.sdn.maintenance_interval
        self.maintenance_ops = []

    def start(self):
        # FIXME(wszumski): Need to implement a clean shutdown instead of using
        # daemon threads. Currently DB operations hang indefinitely in the test
        # framework, preventing tests from running to completition.
        self.maintenance_thread.daemon = True
        self.maintenance_thread.start()

    def _execute_op(self, operation, context):
        op_details = operation.__name__
        if operation.__doc__:
            op_details += " (%s)" % operation.func_doc

        try:
            LOG.info("Starting maintenance operation %s.", op_details)
            db.update_maintenance_operation(context, operation=operation)
            operation(context=context)
            LOG.info("Finished maintenance operation %s.", op_details)
        except Exception:
            LOG.exception("Failed during maintenance operation %s.",
                          op_details)

    def _maintenance_loop(self):
        while True:
            try:
                time.sleep(self.maintenance_interval)
                self.execute_ops()
            except cfg.NotInitializedError:
                # FIXME(wszumski): Added to workaround noise in the
                # test output. I'm assuming we hit this on startup/shutdown as
                # oslo_config is not initialized. Revisit and find a better
                # fix ...
                LOG.debug(
                    "Caught NotInitializedError: Assuming shutdown/startup")

            except Exception as e:
                LOG.exception("Exception in maintenance loop - %s", e)

    def execute_ops(self):
        LOG.info("Starting journal maintenance run.")
        db_context = context.get_admin_context()
        if not db.lock_maintenance(db_context):
            LOG.info("Maintenance already running, aborting.")
            return

        try:
            for operation in self.maintenance_ops:
                self._execute_op(operation, db_context)
        finally:
            db.update_maintenance_operation(db_context, operation=None)
            db.unlock_maintenance(db_context)
            LOG.info("Finished journal maintenance run.")

    def register_operation(self, f):
        """Register a function to be run by the maintenance thread.

        :param f: Function to call when the thread runs. The function will
        receive a DB session to use for DB operations.
        """
        self.maintenance_ops.append(f)
