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
import datetime

from neutron_lib.db import api as db_api
from oslo_db import api as oslo_db_api
from oslo_serialization import jsonutils
from sqlalchemy import asc
from sqlalchemy import func
from sqlalchemy import or_

from networking_mlnx.db.models import sdn_journal_db
from networking_mlnx.db.models import sdn_maintenance_db
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const


@db_api.CONTEXT_READER
def check_for_pending_or_processing_ops(context, object_uuid, operation=None):
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        or_(sdn_journal_db.SdnJournal.state == sdn_const.PENDING,
            sdn_journal_db.SdnJournal.state == sdn_const.PROCESSING),
        sdn_journal_db.SdnJournal.object_uuid == object_uuid)
    if operation:
        if isinstance(operation, (list, tuple)):
            q = q.filter(sdn_journal_db.SdnJournal.operation.in_(operation))
        else:
            q = q.filter(sdn_journal_db.SdnJournal.operation == operation)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def check_for_pending_delete_ops_with_parent(context, object_type, parent_id):
    rows = context.session.query(sdn_journal_db.SdnJournal).filter(
        or_(sdn_journal_db.SdnJournal.state == sdn_const.PENDING,
            sdn_journal_db.SdnJournal.state == sdn_const.PROCESSING),
        sdn_journal_db.SdnJournal.object_type == object_type,
        sdn_journal_db.SdnJournal.operation == sdn_const.DELETE
    ).all()

    for row in rows:
        if parent_id in row.data:
            return True

    return False


@db_api.CONTEXT_READER
def check_for_older_ops(context, row):
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        or_(sdn_journal_db.SdnJournal.state == sdn_const.PENDING,
            sdn_journal_db.SdnJournal.state == sdn_const.PROCESSING),
        sdn_journal_db.SdnJournal.object_uuid == row.object_uuid,
        sdn_journal_db.SdnJournal.created_at < row.created_at,
        sdn_journal_db.SdnJournal.id != row.id)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def get_all_db_rows(context):
    return context.session.query(sdn_journal_db.SdnJournal).all()


@db_api.CONTEXT_READER
def get_all_db_rows_by_state(context, state):
    return context.session.query(sdn_journal_db.SdnJournal).filter_by(
        state=state).all()


def _get_row_with_lock(session):
    row = session.query(sdn_journal_db.SdnJournal).filter_by(
        state=sdn_const.PENDING).order_by(
        asc(sdn_journal_db.SdnJournal.last_retried)).with_for_update(
    ).first()
    return row


# Retry deadlock exception for Galera DB.
# If two (or more) different threads call this method at the same time, they
# might both succeed in changing the same row to pending, but at least one
# of them will get a deadlock from Galera and will have to retry the operation.
@db_api.retry_db_errors
@db_api.CONTEXT_WRITER
def get_oldest_pending_db_row_with_lock(context):
    row = _get_row_with_lock(context.session)
    if row:
        _update_db_row_state(context.session, row, sdn_const.PROCESSING)
    return row


@db_api.retry_db_errors
@db_api.CONTEXT_READER
def get_all_monitoring_db_row_by_oldest(context):
    rows = context.session.query(sdn_journal_db.SdnJournal).filter_by(
        state=sdn_const.MONITORING).order_by(
        asc(sdn_journal_db.SdnJournal.last_retried)).all()
    return rows


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES)
@db_api.CONTEXT_WRITER
def update_db_row_state(context, row, state):
    _update_db_row_state(context.session, row, state)


def _update_db_row_state(session, row, state):
    row.state = state
    session.merge(row)


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES)
@db_api.CONTEXT_WRITER
def update_db_row_job_id(context, row, job_id):
    row.job_id = job_id
    context.session.merge(row)


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES)
@db_api.CONTEXT_WRITER
def update_pending_db_row_retry(context, row, retry_count):
    if row.retry_count >= retry_count and retry_count != -1:
        _update_db_row_state(context.session, row, sdn_const.FAILED)
    else:
        row.retry_count += 1
        _update_db_row_state(context.session, row, sdn_const.PENDING)


# This function is currently not used.
# Deleted resources are marked as 'deleted' in the database.
@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES)
@db_api.CONTEXT_WRITER
def delete_row(context, row=None, row_id=None):
    if row_id:
        row = context.session.query(sdn_journal_db.SdnJournal).filter_by(
            id=row_id).one()
    if row:
        context.session.delete(row)


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES)
@db_api.CONTEXT_WRITER
def create_pending_row(context, object_type, object_uuid,
                       operation, data):
    data = jsonutils.dumps(data)
    row = sdn_journal_db.SdnJournal(object_type=object_type,
                                    object_uuid=object_uuid,
                                    operation=operation, data=data,
                                    created_at=func.now(),
                                    state=sdn_const.PENDING)
    context.session.add(row)


def _update_maintenance_state(session, expected_state, state):
    row = session.query(sdn_maintenance_db.SdnMaintenance).filter_by(
        state=expected_state).with_for_update().one_or_none()
    if row is None:
        return False

    row.state = state
    return True


@db_api.retry_db_errors
@db_api.CONTEXT_WRITER
def lock_maintenance(context):
    return _update_maintenance_state(context.session, sdn_const.PENDING,
                                     sdn_const.PROCESSING)


@db_api.retry_db_errors
@db_api.CONTEXT_WRITER
def unlock_maintenance(context):
    return _update_maintenance_state(context.session, sdn_const.PROCESSING,
                                     sdn_const.PENDING)


@db_api.CONTEXT_WRITER
def update_maintenance_operation(context, operation=None):
    """Update the current maintenance operation details.

    The function assumes the lock is held, so it mustn't be run outside of a
    locked context.
    """
    op_text = None
    if operation:
        op_text = operation.__name__

    row = context.session.query(
        sdn_maintenance_db.SdnMaintenance).one_or_none()
    row.processing_operation = op_text


@db_api.CONTEXT_WRITER
def delete_rows_by_state_and_time(context, state, time_delta):
    now = context.session.execute(func.now()).scalar()
    context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.state == state,
        sdn_journal_db.SdnJournal.last_retried < now - time_delta).delete(
        synchronize_session=False)
    context.session.expire_all()


@db_api.CONTEXT_WRITER
def reset_processing_rows(context, max_timedelta):
    now = context.session.execute(func.now()).scalar()
    max_timedelta = datetime.timedelta(seconds=max_timedelta)
    rows = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.last_retried < now - max_timedelta,
        sdn_journal_db.SdnJournal.state == sdn_const.PROCESSING,
    ).update({'state': sdn_const.PENDING})

    return rows
