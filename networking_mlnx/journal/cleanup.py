# Copyright 2016 Mellanox Technologies, Ltd
#
#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

from datetime import timedelta

from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from networking_mlnx.db import db
from networking_mlnx.plugins.ml2.drivers.sdn import config
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_exc

LOG = logging.getLogger(__name__)
cfg.CONF.register_opts(config.sdn_opts, sdn_const.GROUP_OPT)


class JournalCleanup(object):
    """Journal maintenance operation for deleting completed rows."""
    def __init__(self):
        self._rows_retention = cfg.CONF.sdn.completed_rows_retention
        self._processing_timeout = cfg.CONF.sdn.processing_timeout

    def delete_completed_rows(self, context):
        if self._rows_retention != -1:
            LOG.debug("Deleting completed and failed rows")
            # Failed rows are never retried, so they are kept only for as
            # long as completed ones, except failed port deletions: the
            # port's GUID may still be in its partition key, and while the
            # row exists no other port with that GUID is added to a
            # partition key. An operator deletes the row once the GUID has
            # been removed from the partition key.
            db.delete_rows_by_state_and_time(
                context, sdn_const.COMPLETED,
                timedelta(seconds=self._rows_retention))
            db.delete_rows_by_state_and_time(
                context, sdn_const.FAILED,
                timedelta(seconds=self._rows_retention),
                keep_port_deletes=True)

    def cleanup_processing_rows(self, context):
        row_count = db.reset_processing_rows(context, self._processing_timeout)
        if row_count:
            LOG.info("Reset %(num)s orphaned rows back to pending",
                     {"num": row_count})


class JournalRecovery(object):
    """Journal maintenance operation for sending failed deletions again

    Until the controller has deleted a port, its GUID may still be in its
    partition key, so the journal keeps a failed port deletion, and other
    ports with the same GUID can't be added to a partition key. Sending it
    again is harmless once it is done: the controller then answers 404.
    networking-odl's journal recovery does the same.
    """

    def __init__(self):
        self._interval = cfg.CONF.sdn.failed_port_delete_retry_interval

    def retry_failed_port_deletes(self, context):
        if self._interval == -1:
            return
        rows = db.get_failed_port_deletes(
            context, timedelta(seconds=self._interval))
        for row in rows:
            try:
                self._retry(context, row)
            except Exception:
                # Go on with the other rows. This one is tried again after
                # another interval.
                LOG.exception("Failed to send the failed deletion of port "
                              "%(port)s again (journal row %(row)s)",
                              {'row': row.id, 'port': row.object_uuid})
                self._touch(context, row)

    @staticmethod
    def _touch(context, row):
        try:
            db.touch_failed_row(context, row.id)
        except Exception:
            LOG.exception("Failed to update journal row %s", row.id)

    def _retry(self, context, row):
        log_args = {'row': row.id, 'port': row.object_uuid}
        data = jsonutils.loads(row.data or '{}')
        if not (db.get_port_guid_identity(data) and data.get('network_id')):
            # Rows from earlier releases may not have them, so whether
            # another port uses the GUID can't be checked.
            self._warn(context, row, dict(
                log_args, error="the journal row doesn't record the port's "
                "GUID and network"))
            return
        try:
            db.retry_failed_row(context, row.id)
        except (sdn_exc.JournalRowNotFound, sdn_exc.JournalRowNotFailed):
            # Something else dealt with it meanwhile.
            return
        except (sdn_exc.JournalRowSuperseded,
                sdn_exc.JournalRowNotRetryable) as e:
            self._warn(context, row, dict(log_args, error=e))
            return
        LOG.info("Sending the failed deletion of port %(port)s to the SDN "
                 "controller again (journal row %(row)s)", log_args)

    def _warn(self, context, row, log_args):
        LOG.warning("Not re-sending the failed deletion of port %(port)s: "
                    "%(error)s. Check on the SDN controller that its GUID is "
                    "in no partition key that it shouldn't be in, then "
                    "delete the row with neutron-mlnx-manage journal delete "
                    "%(row)s", log_args)
        # Warn again only after another interval.
        self._touch(context, row)
