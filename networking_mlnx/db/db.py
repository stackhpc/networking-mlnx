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

from neutron.db.models import provisioning_block as pb_model
from neutron.db import models_v2
from neutron_lib.db import api as db_api
from oslo_serialization import jsonutils
from sqlalchemy import and_
from sqlalchemy import asc
from sqlalchemy import func
from sqlalchemy import or_

from networking_mlnx.db.models import sdn_journal_db
from networking_mlnx.db.models import sdn_maintenance_db
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const

# States of operations that the SDN controller may not have finished yet.
# Operations that depend on them must wait, including while the controller
# is still running the job for an accepted request (MONITORING).
_UNFINISHED_STATES = (sdn_const.PENDING, sdn_const.PROCESSING,
                      sdn_const.MONITORING)


@db_api.CONTEXT_READER
def check_for_pending_or_processing_ops(context, object_uuid, operation=None):
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.state.in_(_UNFINISHED_STATES),
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
        sdn_journal_db.SdnJournal.state.in_(_UNFINISHED_STATES),
        sdn_journal_db.SdnJournal.object_type == object_type,
        sdn_journal_db.SdnJournal.operation == sdn_const.DELETE
    ).all()

    for row in rows:
        if parent_id in row.data:
            return True

    return False


def _get_created_at(context, row):
    # Compare with the stored value rather than row.created_at, which the
    # database may format differently when it is sent back as a parameter.
    # SQLite stores it without fractional seconds, but compares it as text
    # with a parameter that has them.
    return context.session.query(
        sdn_journal_db.SdnJournal.created_at).filter(
        sdn_journal_db.SdnJournal.id == row.id).scalar_subquery()


# The rows of an object are ordered by seqnum. Rows recorded before seqnum
# was added have none, so they come before any row that has one, and are
# ordered among themselves by created_at, which only has a resolution of a
# second.

def _is_older(context, row, same_second_is_older):
    journal = sdn_journal_db.SdnJournal
    if row.seqnum is not None:
        return or_(journal.seqnum.is_(None), journal.seqnum < row.seqnum)
    if same_second_is_older:
        created_at = journal.created_at <= _get_created_at(context, row)
    else:
        created_at = journal.created_at < row.created_at
    return and_(journal.seqnum.is_(None), created_at)


def _is_newer(context, row):
    journal = sdn_journal_db.SdnJournal
    if row.seqnum is not None:
        return journal.seqnum > row.seqnum
    # A row created in the same second counts as newer. Callers use this to
    # ignore stale rows, so erring this way can only make them ignore a row.
    return or_(journal.seqnum.isnot(None),
               journal.created_at >= _get_created_at(context, row))


@db_api.CONTEXT_READER
def check_for_older_ops(context, row):
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.state.in_(_UNFINISHED_STATES),
        sdn_journal_db.SdnJournal.object_uuid == row.object_uuid,
        _is_older(context, row, same_second_is_older=False),
        sdn_journal_db.SdnJournal.id != row.id)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def check_for_newer_ops(context, row, operation):
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.object_uuid == row.object_uuid,
        sdn_journal_db.SdnJournal.operation == operation,
        _is_newer(context, row),
        sdn_journal_db.SdnJournal.id != row.id)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def check_for_unfinished_delete_not_newer(context, row):
    # Without seqnum, check_for_older_ops() can't order rows created in the
    # same second, which is the norm for the DELETE of a port's old binding
    # and the POST of its new one. Sending the POST first would let the
    # DELETE remove the port from the partition key after its POST has
    # completed.
    q = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.state.in_(_UNFINISHED_STATES),
        sdn_journal_db.SdnJournal.object_uuid == row.object_uuid,
        sdn_journal_db.SdnJournal.operation == sdn_const.DELETE,
        _is_older(context, row, same_second_is_older=True),
        sdn_journal_db.SdnJournal.id != row.id)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def port_has_provisioning_block(context, port_id, entity):
    q = context.session.query(pb_model.ProvisioningBlock).join(
        models_v2.Port,
        models_v2.Port.standard_attr_id ==
        pb_model.ProvisioningBlock.standard_attr_id).filter(
        models_v2.Port.id == port_id,
        pb_model.ProvisioningBlock.entity == entity)
    return context.session.query(q.exists()).scalar()


@db_api.CONTEXT_READER
def get_all_db_rows(context):
    return context.session.query(sdn_journal_db.SdnJournal).all()


@db_api.CONTEXT_READER
def get_all_db_rows_by_state(context, state):
    return context.session.query(sdn_journal_db.SdnJournal).filter_by(
        state=state).all()


def _retry_due(now, retry_delays):
    """A filter for rows that have no retries or whose retry is due.

    retry_delays[i] is the delay in seconds before retry i + 1. The last
    delay also applies to later retries.
    """
    journal = sdn_journal_db.SdnJournal
    clauses = [journal.retry_count == 0]
    for retry, delay in enumerate(retry_delays, 1):
        retry_match = (journal.retry_count >= retry
                       if retry == len(retry_delays)
                       else journal.retry_count == retry)
        clauses.append(and_(
            retry_match,
            journal.last_retried <= now - datetime.timedelta(seconds=delay)))
    return or_(*clauses)


def _get_row_with_lock(session, exclude_ids=(), retry_delays=()):
    query = session.query(sdn_journal_db.SdnJournal).filter_by(
        state=sdn_const.PENDING)
    if exclude_ids:
        query = query.filter(
            ~sdn_journal_db.SdnJournal.id.in_(exclude_ids))
    if retry_delays:
        now = session.execute(func.now()).scalar()
        query = query.filter(_retry_due(now, retry_delays))
    row = query.order_by(
        asc(sdn_journal_db.SdnJournal.last_retried)).with_for_update(
    ).first()
    return row


# Retry deadlock exception for Galera DB.
# If two (or more) different threads call this method at the same time, they
# might both succeed in changing the same row to pending, but at least one
# of them will get a deadlock from Galera and will have to retry the operation.
@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def get_oldest_pending_db_row_with_lock(context, exclude_ids=()):
    row = _get_row_with_lock(context.session, exclude_ids)
    if row:
        _update_db_row_state(context.session, row, sdn_const.PROCESSING)
    return row


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def get_oldest_valid_pending_db_row_with_lock(context, validate,
                                              exclude_ids=(),
                                              retry_delays=()):
    """Lock the oldest pending row, and mark it processing if it's valid.

    Returns the row, or None if there is no pending row, and the result of
    validate(context, row). A row that has to wait for other operations is
    left unchanged, so that it isn't rewritten on every sync. validate()
    runs while the row is locked. Rows whose retry isn't due yet, as given
    by retry_delays (see _retry_due), are not considered.
    """
    row = _get_row_with_lock(context.session, exclude_ids, retry_delays)
    if not row:
        return None, False
    valid = validate(context, row)
    if valid:
        _update_db_row_state(context.session, row, sdn_const.PROCESSING)
    return row, valid


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_READER
def get_all_monitoring_db_row_by_oldest(context):
    rows = context.session.query(sdn_journal_db.SdnJournal).filter_by(
        state=sdn_const.MONITORING).order_by(
        asc(sdn_journal_db.SdnJournal.last_retried)).all()
    return rows


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def update_db_row_state(context, row, state):
    _update_db_row_state(context.session, row, state)


def _update_db_row_state(session, row, state):
    row.state = state
    session.merge(row)


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def update_db_row_job_id(context, row, job_id):
    row.job_id = job_id
    context.session.merge(row)


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def update_pending_db_row_retry(context, row, retry_count):
    if row.retry_count >= retry_count and retry_count != -1:
        _update_db_row_state(context.session, row, sdn_const.FAILED)
    else:
        row.retry_count += 1
        _update_db_row_state(context.session, row, sdn_const.PENDING)


# This function is currently not used.
# Deleted resources are marked as 'deleted' in the database.
@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def delete_row(context, row=None, row_id=None):
    if row_id:
        row = context.session.query(sdn_journal_db.SdnJournal).filter_by(
            id=row_id).one()
    if row:
        context.session.delete(row)


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def create_pending_row(context, object_type, object_uuid,
                       operation, data):
    data = jsonutils.dumps(data)
    # This runs in the transaction that changes the object, so changes to
    # one object get consecutive numbers. If concurrent transactions still
    # pick the same number, the unique constraint fails one of them, and it
    # is retried.
    last_seqnum = context.session.query(
        func.max(sdn_journal_db.SdnJournal.seqnum)).filter(
        sdn_journal_db.SdnJournal.object_uuid == object_uuid).scalar()
    row = sdn_journal_db.SdnJournal(object_type=object_type,
                                    object_uuid=object_uuid,
                                    operation=operation, data=data,
                                    created_at=func.now(),
                                    state=sdn_const.PENDING,
                                    seqnum=(last_seqnum or 0) + 1)
    context.session.add(row)


def _update_maintenance_state(session, expected_state, state):
    row = session.query(sdn_maintenance_db.SdnMaintenance).filter_by(
        state=expected_state).with_for_update().one_or_none()
    if row is None:
        return False

    row.state = state
    return True


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def lock_maintenance(context):
    return _update_maintenance_state(context.session, sdn_const.PENDING,
                                     sdn_const.PROCESSING)


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def unlock_maintenance(context):
    return _update_maintenance_state(context.session, sdn_const.PROCESSING,
                                     sdn_const.PENDING)


@db_api.retry_if_session_inactive()
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


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def delete_rows_by_state_and_time(context, state, time_delta):
    now = context.session.execute(func.now()).scalar()
    context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.state == state,
        sdn_journal_db.SdnJournal.last_retried < now - time_delta).delete(
        synchronize_session=False)
    context.session.expire_all()


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_WRITER
def reset_processing_rows(context, max_timedelta):
    now = context.session.execute(func.now()).scalar()
    max_timedelta = datetime.timedelta(seconds=max_timedelta)
    rows = context.session.query(sdn_journal_db.SdnJournal).filter(
        sdn_journal_db.SdnJournal.last_retried < now - max_timedelta,
        sdn_journal_db.SdnJournal.state == sdn_const.PROCESSING,
    ).update({'state': sdn_const.PENDING})

    return rows
