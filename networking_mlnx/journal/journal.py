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

import functools
import html.parser
import re
import threading
from urllib import parse

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


# The job links that UFM's HTML replies contain, such as /app/jobs/1. They
# are added to the controller's URL, so nothing else is accepted.
JOB_LINK_RE = re.compile(r'[a-zA-Z0-9/]+')
# The length of the sdn_journal.job_id column.
MAX_JOB_ID_LENGTH = 36


class _LinkParser(html.parser.HTMLParser):
    """Find the target of the last link in an HTML page

    The code that NEO needed, and that this replaces, used the page's last
    start tag, if it was a link.
    """

    def __init__(self):
        super().__init__()
        self.href = None

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href') if tag == 'a' else None
        if href:
            self.href = href


def _get_job_id(response):
    """Return the job that a response to a request names, or None

    UFM answers with the job as JSON, except for some requests, such as a
    port's POST, which it answers with an HTML page that links to the job.
    A link is returned relative to the controller's URL ([sdn] url).
    """
    try:
        return response.json()
    except ValueError:
        pass
    parser = _LinkParser()
    parser.feed(response.text)
    parser.close()
    link = parser.href
    if not link or not JOB_LINK_RE.fullmatch(link):
        link = None
    else:
        # The job is fetched relative to the controller's URL, so remove
        # its path, such as /ufmRestV3, from a link that starts with it.
        prefix = parse.urlsplit(cfg.CONF.sdn.url).path.rstrip('/') + '/'
        if prefix != '/' and link.startswith(prefix):
            link = link[len(prefix):]
        link = link.strip('/')
    if link and len(link) <= MAX_JOB_ID_LENGTH:
        return link
    # The caller logs the response.
    LOG.debug("SDN Controller response is neither JSON nor HTML that links "
              "to a usable job")
    return None


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
    reason = None
    if not host:
        reason = "it was sent without a host"
    elif not db.port_has_provisioning_block(
            context, port_id, sdn_const.PROVISIONING_ENTITY):
        reason = "the port has no SDN provisioning block"
    else:
        bound_host = ml2_db.get_port_binding_host(context, port_id)
        if bound_host != host:
            reason = ("the port is now bound to host %s, not %s" %
                      (bound_host, host))
        elif db.check_for_newer_ops(context, row, sdn_const.POST):
            reason = "a newer POST of the port is in the journal"
    if reason:
        LOG.debug("Journal row %(row)s doesn't hold port %(port)s DOWN: "
                  "%(reason)s", {'row': row.id, 'port': port_id,
                                 'reason': reason})
        return None
    return port_id


def _get_blocked_port_id_locked(context, row):
    """Like _get_blocked_port_id(), but lock the port's binding first

    The caller must be in a writer transaction. Until it ends, a concurrent
    unbind, rebind or move of the port waits, so the port is still the one
    that the row holds when the caller acts on it.
    """
    if not _lock_port_binding_of_post(context, row):
        return None
    return _get_blocked_port_id(context, row)


def _lock_port_binding_of_post(context, row):
    """Lock the binding of the port that a port's POST row sends

    Lock it before anything else is read, so that the reads that follow see
    any change that committed while this waited for the lock.

    :returns: True if the row is a port's POST, so that the binding is
              locked
    """
    if (row.object_type != sdn_const.PORT or
            row.operation != sdn_const.POST):
        return False
    db.lock_port_binding(context, row.object_uuid)
    return True


@db_api.retry_if_session_inactive()
def _complete_row(context, row):
    """Release the port that a MONITORING row holds, if any, and complete it

    Both happen in one transaction, which locks the row, so that a journal
    thread that read the row before another one changed it can't act on
    it.

    :returns: the completed row, or None if the row has changed since it
              was read, and the ID of the port released, or None
    """
    with db_api.CONTEXT_WRITER.using(context):
        # Lock the port's binding before the row, in the order that ML2
        # locks them when it records a row, so that neither waits for the
        # other.
        _lock_port_binding_of_post(context, row)
        locked = db.lock_monitoring_row(context, row)
        if not locked:
            return None, None
        port_id = _get_blocked_port_id(context, locked)
        if port_id:
            provisioning_blocks.remove_provisioning_component(
                context, port_id, resources.PORT,
                sdn_const.PROVISIONING_ENTITY)
        db.update_db_row_state(context, locked, sdn_const.COMPLETED)
    return locked, port_id


@db_api.retry_if_session_inactive()
def _update_monitored_row(context, row, update):
    """Call update(context, row) on a MONITORING row, while it is locked

    :returns: the row as updated, or None if the row has changed since it
              was read, so that it was left alone
    """
    with db_api.CONTEXT_WRITER.using(context):
        locked = db.lock_monitoring_row(context, row)
        if locked:
            update(context, locked)
    return locked


def _log_row_changed(row):
    LOG.debug("Journal row %(row)s for %(operation)s %(type)s %(uuid)s has "
              "changed since job %(job_id)s was checked, so another journal "
              "thread has handled it", {'row': row.id,
                                        'operation': row.operation,
                                        'type': row.object_type,
                                        'uuid': row.object_uuid,
                                        'job_id': row.job_id})


@db_api.retry_if_session_inactive()
def _set_port_provisioned(context, port_id):
    # Nothing is left to remove, but this makes the port ACTIVE if no
    # other provisioning blocks remain.
    provisioning_blocks.provisioning_complete(
        context, port_id, resources.PORT, sdn_const.PROVISIONING_ENTITY)


def _complete_monitored_row(context, row):
    """Complete a row whose job has completed, and release its port

    If releasing the port fails, the row stays MONITORING, so that this is
    tried again at the next sync.
    """
    try:
        completed, port_id = _complete_row(context, row)
    except Exception:
        # A port that the row holds stays DOWN until this is tried again.
        LOG.exception("Failed to complete %(operation)s %(type)s %(uuid)s",
                      {'operation': row.operation, 'type': row.object_type,
                       'uuid': row.object_uuid})
        return
    if not completed:
        _log_row_changed(row)
        return
    if not port_id:
        return
    LOG.info("SDN Controller added port %s to its partition key, "
             "released it", port_id)
    try:
        _set_port_provisioned(context, port_id)
    except Exception:
        # The block is gone, so trying again would find nothing to release.
        # The port stays DOWN, so Nova and Ironic fail when they time out.
        LOG.exception("Failed to make port %s ACTIVE", port_id)


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

            if (row.object_type == sdn_const.PORT and
                    row.operation == sdn_const.POST and
                    _get_blocked_port_id(context, row) and
                    db.check_for_failed_delete_of_same_guid(context, row)):
                # The GUID may still be in the partition key of the port
                # whose deletion failed, so this port must not be used.
                LOG.error("Not adding port %s to its partition key: the SDN "
                          "Controller failed to delete another port with "
                          "the same GUID, which may still be in another "
                          "partition", row.object_uuid)
                self._fail_row(context, row)
                continue

            LOG.info("Syncing %(operation)s %(type)s %(uuid)s (journal row "
                     "%(row)s, retry %(retry)s)",
                     {'operation': row.operation, 'type': row.object_type,
                      'uuid': row.object_uuid, 'row': row.id,
                      'retry': row.retry_count})

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
                                  "%(operation)s %(type)s %(uuid)s: "
                                  "%(response)s",
                                  {'operation': row.operation,
                                   'type': row.object_type,
                                   'uuid': row.object_uuid,
                                   'response':
                                       self.client.describe_response(
                                           response)})
                        self._fail_row(context, row)
                    else:
                        LOG.info("SDN Controller does not implement "
                                 "%(operation)s %(type)s %(uuid)s, "
                                 "skipping it",
                                 {'operation': row.operation,
                                  'type': row.object_type,
                                  'uuid': row.object_uuid})
                        db.update_db_row_state(
                            context, row, sdn_const.COMPLETED)
                elif (response.status_code == requests.codes.not_found and
                      row.operation == sdn_const.DELETE):
                    LOG.info("SDN Controller does not have %(type)s "
                             "%(uuid)s, so it is already deleted",
                             {'type': row.object_type,
                              'uuid': row.object_uuid})
                    db.update_db_row_state(context, row, sdn_const.COMPLETED)
                else:
                    # update in progress and job_id
                    job_id = _get_job_id(response)

                    if job_id:
                        LOG.info("SDN Controller started job %(job_id)s "
                                 "for %(operation)s %(type)s %(uuid)s",
                                 {'job_id': job_id,
                                  'operation': row.operation,
                                  'type': row.object_type,
                                  'uuid': row.object_uuid})
                        db.update_db_row_job_id(
                            context, row, job_id=job_id)
                        db.update_db_row_state(
                            context, row, sdn_const.MONITORING)
                    elif _get_blocked_port_id(context, row):
                        # Without a job, there is no way to tell when the
                        # port has been added to its partition key.
                        LOG.error("SDN Controller returned no job for "
                                  "%(operation)s %(type)s %(uuid)s, so "
                                  "there is no way to tell when the port is "
                                  "in its partition key: %(response)s",
                                  {'operation': row.operation,
                                   'type': row.object_type,
                                   'uuid': row.object_uuid,
                                   'response':
                                       self.client.describe_response(
                                           response)})
                        self._fail_row(context, row)
                    elif row.operation == sdn_const.DELETE:
                        # Nothing shows the deletion was done, and until it
                        # is, a port's GUID may still be in its partition
                        # key. Sending it again is harmless: once it is
                        # done, the controller answers 404.
                        LOG.warning("SDN Controller returned no job for "
                                    "%(operation)s %(type)s %(uuid)s, "
                                    "sending it again: %(response)s",
                                    {'operation': row.operation,
                                     'type': row.object_type,
                                     'uuid': row.object_uuid,
                                     'response':
                                         self.client.describe_response(
                                             response)})
                        self._retry_row(context, row)
                    else:
                        # The controller accepted the request but gave no
                        # job to monitor. Sending it again would only make
                        # the controller do it again.
                        LOG.warning("SDN Controller returned no job for "
                                    "%(operation)s %(type)s %(uuid)s, "
                                    "assuming it is done: %(response)s",
                                    {'operation': row.operation,
                                     'type': row.object_type,
                                     'uuid': row.object_uuid,
                                     'response':
                                         self.client.describe_response(
                                             response)})
                        db.update_db_row_state(
                            context, row, sdn_const.COMPLETED)
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
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError) as e:
                # Log an error and raise the retry count. If the retry count
                # exceeds the limit, move it to the failed state.
                LOG.error("Failed to send %(operation)s %(type)s %(uuid)s to "
                          "the SDN Controller: %(error)s",
                          {'operation': row.operation,
                           'type': row.object_type,
                           'uuid': row.object_uuid, 'error': e})
                self._retry_row(context, row)
                # Break out of the loop and retry with the next
                # timer interval
                break

    def _retry_row(self, context, row, monitored=False):
        """Count a retry of the row, and fail it after too many

        :param monitored: the row is MONITORING, so another journal thread
                          may have changed it since it was read
        """
        if monitored:
            updated = _update_monitored_row(
                context, row, functools.partial(
                    db.update_pending_db_row_retry,
                    retry_count=self._row_retry_count))
            if not updated:
                _log_row_changed(row)
                return
            row = updated
        else:
            db.update_pending_db_row_retry(context, row,
                                           self._row_retry_count)
        log_args = {'operation': row.operation, 'type': row.object_type,
                    'uuid': row.object_uuid, 'retry': row.retry_count,
                    'max': (self._row_retry_count
                            if self._row_retry_count != -1 else 'unlimited')}
        if row.state == sdn_const.FAILED:
            LOG.error("Giving up on %(operation)s %(type)s %(uuid)s after "
                      "%(retry)s retries", log_args)
            _fail_port_provisioning(context, row)
        else:
            LOG.info("Will send %(operation)s %(type)s %(uuid)s again "
                     "(retry %(retry)s of %(max)s)", log_args)

    @staticmethod
    def _fail_row(context, row, monitored=False):
        """Fail the row, and the port that it holds DOWN, if any

        :param monitored: as for _retry_row()
        """
        if monitored:
            failed = _update_monitored_row(
                context, row, functools.partial(
                    db.update_db_row_state, state=sdn_const.FAILED))
            if not failed:
                _log_row_changed(row)
                return
        else:
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
                            LOG.info("SDN Controller job %(job_id)s for "
                                     "%(operation)s %(type)s %(uuid)s "
                                     "completed",
                                     {'job_id': row.job_id,
                                      'operation': row.operation,
                                      'type': row.object_type,
                                      'uuid': row.object_uuid})
                            _complete_monitored_row(context, row)
                            continue
                        if job_status in ("Pending", "Running"):
                            LOG.debug("SDN Controller Job id %(job_id)s is "
                                      "%(status)s continue monitoring",
                                      {'job_id': row.job_id,
                                       'status': job_status})
                            continue
                        # Send the request again, up to retry_count times,
                        # as for other failures.
                        LOG.error("SDN Controller job %(job_id)s for "
                                  "%(operation)s %(type)s %(uuid)s failed "
                                  "with status %(status)s: %(response)s",
                                  {'job_id': row.job_id,
                                   'operation': row.operation,
                                   'type': row.object_type,
                                   'uuid': row.object_uuid,
                                   'status': job_status,
                                   'response':
                                       self.client.describe_response(
                                           response)})
                        if _get_blocked_port_id(context, row):
                            # Fail the instance's boot rather than retry
                            # while Nova waits.
                            self._fail_row(context, row, monitored=True)
                        else:
                            self._retry_row(context, row, monitored=True)
                    except (ValueError, AttributeError):
                        # Send the request again, up to retry_count times,
                        # rather than monitor a job that can't be read,
                        # because other operations may be waiting for it.
                        LOG.error("Failed to read the status of SDN "
                                  "Controller job %(job_id)s for "
                                  "%(operation)s %(type)s %(uuid)s: "
                                  "%(response)s",
                                  {'job_id': row.job_id,
                                   'operation': row.operation,
                                   'type': row.object_type,
                                   'uuid': row.object_uuid,
                                   'response':
                                       self.client.describe_response(
                                           response)})
                        self._retry_row(context, row, monitored=True)
                else:
                    LOG.error("Failed to get SDN Controller job "
                              "%(job_id)s for %(operation)s %(type)s "
                              "%(uuid)s: %(response)s",
                              {'job_id': row.job_id,
                               'operation': row.operation,
                               'type': row.object_type,
                               'uuid': row.object_uuid,
                               'response':
                                   self.client.describe_response(response)})
                    self._retry_row(context, row, monitored=True)

            except sdn_exc.SDNRequestError as e:
                LOG.error("Failed to get SDN Controller job %(job_id)s for "
                          "%(operation)s %(type)s %(uuid)s: %(error)s",
                          self._job_log_args(row, e))
                self._retry_row(context, row, monitored=True)
            except sdn_exc.SDNUnreachableError as e:
                # The job may well be fine, so keep monitoring it rather
                # than send the request again, which would start another
                # job. Check it again at the next timer interval.
                LOG.error("Cannot connect to the SDN Controller to check "
                          "job %(job_id)s for %(operation)s %(type)s "
                          "%(uuid)s: %(error)s", self._job_log_args(row, e))
                break
            except (sdn_exc.SDNConnectionError, sdn_exc.SDNLoginError) as e:
                # The controller answered with an error, for example because
                # it no longer knows the job. Send the request again, up to
                # retry_count times, rather than keep checking a job that
                # can't be checked while other rows wait.
                LOG.error("Failed to get SDN Controller job %(job_id)s for "
                          "%(operation)s %(type)s %(uuid)s: %(error)s",
                          self._job_log_args(row, e))
                self._retry_row(context, row, monitored=True)

    @staticmethod
    def _job_log_args(row, error):
        return {'job_id': row.job_id, 'operation': row.operation,
                'type': row.object_type, 'uuid': row.object_uuid,
                'error': error}
