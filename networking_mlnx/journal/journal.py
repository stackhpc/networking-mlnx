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

import re
import threading

from neutron.db import provisioning_blocks
from neutron.plugins.ml2 import db as ml2_db
from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as nl_context
from neutron_lib.db import api as db_api
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
import requests
from six.moves import html_parser

from networking_mlnx.db import db
from networking_mlnx.journal import dependency_validations
from networking_mlnx.plugins.ml2.drivers.sdn import client
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_exc
from networking_mlnx.plugins.ml2.drivers.sdn import utils as sdn_utils

LOG = logging.getLogger(__name__)

# The longest time to wait before sending a failed request again, unless
# [sdn] sync_timeout is longer.
MAX_RETRY_DELAY = 300


def call_thread_on_end(func):
    def new_func(obj, *args, **kwargs):
        return_value = func(obj, *args, **kwargs)
        obj.journal.set_sync_event()
        return return_value
    return new_func


def record(context, object_type, object_uuid, operation, data):
    db.create_pending_row(context, object_type, object_uuid, operation,
                          data)


def _get_blocked_port_id(context, row):
    """Return the ID of the port that the row holds DOWN, or None

    The SDN mechanism driver holds an instance's port DOWN with a
    provisioning block until its POST row has added the port to the
    partition key. Only the latest POST for the port's current binding
    holds the port: a row for an earlier binding must neither release nor
    fail it.
    """
    if (row.object_type != sdn_const.PORT or
            row.operation != sdn_const.POST):
        return None
    port_id = row.object_uuid
    host = jsonutils.loads(row.data).get(portbindings.HOST_ID)
    if (not host or
            not db.port_has_provisioning_block(
                context, port_id, sdn_const.PROVISIONING_ENTITY) or
            ml2_db.get_port_binding_host(context, port_id) != host or
            db.check_for_newer_ops(context, row, sdn_const.POST)):
        return None
    return port_id


def _get_blocked_port_id_locked(context, row):
    """Like _get_blocked_port_id(), but lock the port's binding first

    The caller must be in a writer transaction. Until it ends, a concurrent
    unbind, rebind or move of the port waits, so the port is still the one
    that the row holds when the caller acts on it.
    """
    if (row.object_type != sdn_const.PORT or
            row.operation != sdn_const.POST):
        return None
    # Lock before anything else is read, so that the reads that follow see
    # any change that committed while this waited for the lock.
    db.lock_port_binding(context, row.object_uuid)
    return _get_blocked_port_id(context, row)


def _complete_port_provisioning(context, row):
    try:
        with db_api.CONTEXT_WRITER.using(context):
            port_id = _get_blocked_port_id_locked(context, row)
            if not port_id:
                return
            provisioning_blocks.remove_provisioning_component(
                context, port_id, resources.PORT,
                sdn_const.PROVISIONING_ENTITY)
        LOG.info("SDN Controller added port %s to its partition key",
                 port_id)
        # Nothing is left to remove, but this makes the port ACTIVE if no
        # other provisioning blocks remain.
        provisioning_blocks.provisioning_complete(
            context, port_id, resources.PORT, sdn_const.PROVISIONING_ENTITY)
    except Exception:
        # The port stays DOWN, so Nova fails the boot when it times out.
        LOG.exception("Failed to complete provisioning of port %s",
                      row.object_uuid)


def _fail_port_provisioning(context, row):
    try:
        with db_api.CONTEXT_WRITER.using(context):
            port_id = _get_blocked_port_id_locked(context, row)
        if not port_id:
            return
        # Keep the provisioning block, so that the port can't become ACTIVE
        # when the agent reports it up.
        LOG.error("SDN Controller failed to add port %s to its partition "
                  "key, setting the port to ERROR", port_id)
        directory.get_plugin().update_port_status(
            context, port_id, n_const.PORT_STATUS_ERROR)
    except Exception:
        # The port stays DOWN, so Nova fails the boot when it times out.
        LOG.exception("Failed to set port %s to ERROR", row.object_uuid)


class SdnJournalThread(object):
    """Thread worker for the SDN Journal Database."""

    def __init__(self):
        self.client = client.SdnRestClient.create_client()
        self._sync_timeout = cfg.CONF.sdn.sync_timeout
        self._row_retry_count = cfg.CONF.sdn.retry_count
        self._retry_delays = self._get_retry_delays(self._sync_timeout)
        self.event = threading.Event()
        self.lock = threading.Lock()
        self._sync_thread = self.start_sync_thread()
        self._start_sync_timer()

    def start_sync_thread(self):
        # Start the sync thread
        LOG.debug("Starting a new sync thread")
        sync_thread = threading.Thread(
            name='sync',
            target=self.run_sync_thread)
        sync_thread.start()
        return sync_thread

    def set_sync_event(self):
        # Prevent race when starting the timer
        with self.lock:
            LOG.debug("Resetting thread timer")
            main_thread = threading.main_thread()

            self._timer.cancel()
            if main_thread.is_alive():
                self._start_sync_timer()
            else:
                LOG.debug("Shutting down timer")
        self.event.set()

    def _start_sync_timer(self):
        self._timer = threading.Timer(self._sync_timeout,
                                      self.set_sync_event)
        self._timer.start()

    def run_sync_thread(self, exit_after_run=False):
        while True:
            try:
                self.event.wait()
                main_thread = threading.main_thread()
                self.event.clear()
                if not main_thread.is_alive():
                    LOG.debug("Shutting down sync thread")
                    return

                context = nl_context.get_admin_context()
                self._sync_pending_rows(context, exit_after_run)
                self._sync_progress_rows(context)

                LOG.debug("Clearing sync thread event")
                if exit_after_run:
                    # Permanently waiting thread model breaks unit tests
                    # Adding this arg to exit here only for unit tests
                    break
            except Exception:
                # Catch exceptions to protect the thread while running
                LOG.exception("Error on run_sync_thread")

    @staticmethod
    def _get_retry_delays(sync_timeout):
        """The minimum time to wait before each retry, in seconds.

        It starts at sync_timeout and doubles with each retry, up to
        MAX_RETRY_DELAY or sync_timeout if that is longer, so that a request
        that keeps failing isn't sent again on every sync. The last delay
        applies to all later retries.
        """
        delays = []
        delay = max(sync_timeout, 1)
        max_delay = max(MAX_RETRY_DELAY, delay)
        while delay < max_delay:
            delays.append(delay)
            delay *= 2
        delays.append(max_delay)
        return delays

    def _sync_pending_rows(self, context, exit_after_run):
        # IDs of rows that have to wait for another operation. Don't fetch
        # them again in this run, so that it ends when every remaining row
        # is waiting, and _sync_progress_rows can check the jobs that they
        # are waiting for.
        skipped_row_ids = set()
        while True:
            LOG.debug("sync_pending_rows operation walking database")
            row, valid = db.get_oldest_valid_pending_db_row_with_lock(
                context, dependency_validations.validate,
                exclude_ids=skipped_row_ids,
                retry_delays=self._retry_delays)
            if not row:
                LOG.debug("No rows to sync")
                break

            if not valid:
                # The row stays pending, unchanged.
                LOG.info("%(operation)s %(type)s %(uuid)s has to wait, "
                         "skipping for now",
                         {'operation': row.operation,
                          'type': row.object_type,
                          'uuid': row.object_uuid})
                if exit_after_run:
                    break
                skipped_row_ids.add(row.id)
                continue

            LOG.info("Syncing %(operation)s %(type)s %(uuid)s",
                     {'operation': row.operation, 'type': row.object_type,
                      'uuid': row.object_uuid})

            # Add code to sync this to SDN controller
            urlpath = sdn_utils.strings_to_url(row.object_type)
            if row.operation != sdn_const.POST:
                urlpath = sdn_utils.strings_to_url(urlpath, row.object_uuid)
            try:
                client_operation_method = (
                    getattr(self.client, row.operation.lower()))
                response = (
                    client_operation_method(
                        urlpath, jsonutils.loads(row.data)))
                if response.status_code == requests.codes.not_implemented:
                    if _get_blocked_port_id(context, row):
                        # The port has not been added to its partition key.
                        LOG.error("SDN Controller does not implement "
                                  "%(operation)s %(type)s %(uuid)s",
                                  {'operation': row.operation,
                                   'type': row.object_type,
                                   'uuid': row.object_uuid})
                        self._fail_row(context, row)
                    else:
                        db.update_db_row_state(
                            context, row, sdn_const.COMPLETED)
                elif (response.status_code == requests.codes.not_found and
                      row.operation == sdn_const.DELETE):
                    db.update_db_row_state(context, row, sdn_const.COMPLETED)
                else:
                    # update in progress and job_id
                    job_id = None
                    try:
                        try:
                            job_id = response.json()
                        except ValueError:
                            # Note(moshele) workaround for NEO
                            # because for POST port it return html
                            # and not json
                            parser = html_parser.HTMLParser()
                            parser.feed(response.text)
                            parser.handle_starttag('a', [])
                            url = parser.get_starttag_text()
                            match = re.match(
                                r'<a href="([a-zA-Z0-9\/]+)">', url)
                            if match:
                                job_id = match.group(1)
                    except Exception as e:
                        LOG.error("Failed to extract job_id %s", e)

                    if job_id:
                        db.update_db_row_job_id(
                            context, row, job_id=job_id)
                        db.update_db_row_state(
                            context, row, sdn_const.MONITORING)
                    else:
                        LOG.warning("object %s has NULL job_id",
                                    row.object_uuid)
                        # Without a job, there is no way to tell when the
                        # port has been added to its partition key.
                        if _get_blocked_port_id(context, row):
                            self._fail_row(context, row)
            except sdn_exc.SDNRequestError as e:
                log_args = {'operation': row.operation,
                            'type': row.object_type,
                            'uuid': row.object_uuid, 'error': e}
                if row.operation == sdn_const.DELETE:
                    # A rejected deletion can depend on the controller's
                    # state. For example, UFM rejects deleting a network
                    # while hosts are still associated with it. So retry it,
                    # as for connection errors.
                    LOG.error("SDN Controller rejected %(operation)s "
                              "%(type)s %(uuid)s, retrying: %(error)s",
                              log_args)
                    self._retry_row(context, row)
                    break
                # The controller rejected the request as invalid. Sending it
                # again would be rejected again, and would hold up the rows
                # behind it.
                LOG.error("SDN Controller rejected %(operation)s %(type)s "
                          "%(uuid)s, not retrying: %(error)s", log_args)
                self._fail_row(context, row)
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError):
                # Log an error and raise the retry count. If the retry count
                # exceeds the limit, move it to the failed state.
                LOG.error("Cannot connect to the SDN Controller")
                self._retry_row(context, row)
                # Break out of the loop and retry with the next
                # timer interval
                break

    def _retry_row(self, context, row):
        db.update_pending_db_row_retry(context, row, self._row_retry_count)
        if row.state == sdn_const.FAILED:
            _fail_port_provisioning(context, row)

    @staticmethod
    def _fail_row(context, row):
        db.update_db_row_state(context, row, sdn_const.FAILED)
        _fail_port_provisioning(context, row)

    def _sync_progress_rows(self, context):
        # 1. get all progressed job
        # 2. get status for SDN Controller
        # 3. Update status if completed/failed
        LOG.debug("sync_progress_rows operation walking database")
        rows = db.get_all_monitoring_db_row_by_oldest(context)
        if not rows:
            LOG.debug("No rows to sync")
            return
        for row in rows:
            try:
                if row.job_id is None:
                    LOG.warning("object %s has NULL job_id",
                                row.object_uuid)
                    continue
                response = self.client.get(row.job_id.strip("/"))
                if response:
                    try:
                        job_status = response.json().get('Status')
                        if job_status == 'Completed':
                            db.update_db_row_state(
                                context, row, sdn_const.COMPLETED)
                            _complete_port_provisioning(context, row)
                            continue
                        if job_status in ("Pending", "Running"):
                            LOG.debug("SDN Controller Job id %(job_id)s is "
                                      "%(status)s continue monitoring",
                                      {'job_id': row.job_id,
                                       'status': job_status})
                            continue
                        # Send the request again, up to retry_count times,
                        # as for other failures.
                        LOG.error("SDN Controller Job id %(job_id)s, "
                                  "failed with %(status)s",
                                  {'job_id': row.job_id,
                                  'status': job_status})
                        if _get_blocked_port_id(context, row):
                            # Fail the instance's boot rather than retry
                            # while Nova waits.
                            self._fail_row(context, row)
                        else:
                            self._retry_row(context, row)
                    except (ValueError, AttributeError):
                        # Send the request again, up to retry_count times,
                        # rather than monitor a job that can't be read,
                        # because other operations may be waiting for it.
                        LOG.error("failed to extract response for job "
                                  "id %s", row.job_id)
                        self._retry_row(context, row)
                else:
                    LOG.error("SDN Controller Job id %(job_id)s, failed with "
                              "HTTP status %(status)s",
                              {'job_id': row.job_id,
                               'status': response.status_code})
                    self._retry_row(context, row)

            except sdn_exc.SDNRequestError as e:
                LOG.error("Failed to get SDN Controller job %(job_id)s: "
                          "%(error)s", {'job_id': row.job_id, 'error': e})
                self._retry_row(context, row)
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError):
                # Don't raise the retry count, just log an error
                LOG.error("Cannot connect to the SDN Controller")
                db.update_db_row_state(context, row, sdn_const.PENDING)
                # Break out of the loop and retry with the next
                # timer interval
                break
