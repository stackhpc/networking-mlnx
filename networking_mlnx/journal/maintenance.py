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

from neutron_lib import context
from neutron_lib.db import api as neutron_db_api
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall

from networking_mlnx.db import db


LOG = logging.getLogger(__name__)


class MaintenanceThread(object):
    def __init__(self):
        self.timer = loopingcall.FixedIntervalLoopingCall(self.execute_ops)
        self.maintenance_interval = cfg.CONF.sdn.maintenance_interval
        self.maintenance_ops = []

    def start(self):
        self.timer.start(self.maintenance_interval, stop_on_exception=False)

    @neutron_db_api.CONTEXT_READER
    def _execute_op(self, operation, context):
        op_details = operation.__name__
        if operation.__doc__:
            op_details += " (%s)" % operation.func_doc

        try:
            LOG.info("Starting maintenance operation %s.", op_details)
            db.update_maintenance_operation(context, operation=operation)
            operation(session=context.session)
            LOG.info("Finished maintenance operation %s.", op_details)
        except Exception:
            LOG.exception("Failed during maintenance operation %s.",
                          op_details)

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
