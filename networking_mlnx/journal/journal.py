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

from neutron_lib import context as nl_context
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


def call_thread_on_end(func):
    def new_func(obj, *args, **kwargs):
        return_value = func(obj, *args, **kwargs)
        obj.journal.set_sync_event()
        return return_value
    return new_func


def record(context, object_type, object_uuid, operation, data):
    db.create_pending_row(context, object_type, object_uuid, operation,
                          data)


class SdnJournalThread(object):
    """Thread worker for the SDN Journal Database."""

    def __init__(self):
        self.client = client.SdnRestClient.create_client()
        self._sync_timeout = cfg.CONF.sdn.sync_timeout
        self._row_retry_count = cfg.CONF.sdn.retry_count
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
                exclude_ids=skipped_row_ids)
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
                    db.update_db_row_state(context, row, sdn_const.COMPLETED)
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
                    db.update_pending_db_row_retry(context, row,
                                                   self._row_retry_count)
                    break
                # The controller rejected the request as invalid. Sending it
                # again would be rejected again, and would hold up the rows
                # behind it.
                LOG.error("SDN Controller rejected %(operation)s %(type)s "
                          "%(uuid)s, not retrying: %(error)s", log_args)
                db.update_db_row_state(context, row, sdn_const.FAILED)
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError):
                # Log an error and raise the retry count. If the retry count
                # exceeds the limit, move it to the failed state.
                LOG.error("Cannot connect to the SDN Controller")
                db.update_pending_db_row_retry(context, row,
                                               self._row_retry_count)
                # Break out of the loop and retry with the next
                # timer interval
                break

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
                            continue
                        if job_status in ("Pending", "Running"):
                            LOG.debug("SDN Controller Job id %(job_id)s is "
                                      "%(status)s continue monitoring",
                                      {'job_id': row.job_id,
                                       'status': job_status})
                            continue
                        LOG.error("SDN Controller Job id %(job_id)s, "
                                  "failed with %(status)s",
                                  {'job_id': row.job_id,
                                  'status': job_status})
                        db.update_db_row_state(
                            context, row, sdn_const.PENDING)
                    except (ValueError, AttributeError):
                        # Send the request again, up to retry_count times,
                        # rather than monitor a job that can't be read,
                        # because other operations may be waiting for it.
                        LOG.error("failed to extract response for job "
                                  "id %s", row.job_id)
                        db.update_pending_db_row_retry(
                            context, row, self._row_retry_count)
                else:
                    LOG.error("SDN Controller Job id %(job_id)s, failed with "
                              "HTTP status %(status)s",
                              {'job_id': row.job_id,
                               'status': response.status_code})
                    db.update_pending_db_row_retry(context, row,
                                                   self._row_retry_count)

            except sdn_exc.SDNRequestError as e:
                LOG.error("Failed to get SDN Controller job %(job_id)s: "
                          "%(error)s", {'job_id': row.job_id, 'error': e})
                db.update_pending_db_row_retry(context, row,
                                               self._row_retry_count)
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError):
                # Don't raise the retry count, just log an error
                LOG.error("Cannot connect to the SDN Controller")
                db.update_db_row_state(context, row, sdn_const.PENDING)
                # Break out of the loop and retry with the next
                # timer interval
                break
