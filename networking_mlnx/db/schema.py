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

from neutron_lib.db import api as db_api
from neutron_lib import exceptions as exc
import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from networking_mlnx._i18n import _
from networking_mlnx.db.models import sdn_journal_db


class JournalSchemaError(exc.NeutronException):
    message = _("The networking-mlnx database schema is out of date: "
                "%(reason)s. Upgrade it with 'neutron-db-manage "
                "--subproject networking-mlnx upgrade head'.")


def check_journal_states():
    """Check that the database accepts every SDN journal state.

    The states are a database enum. If a migration that adds one was
    missed, writing that state fails, and the journal's rows never
    complete, so they are retried and kept forever.
    """
    table = sdn_journal_db.SdnJournal.__tablename__
    engine = db_api.CONTEXT_WRITER.get_engine()
    try:
        columns = sa.inspect(engine).get_columns(table)
    except sa_exc.NoSuchTableError:
        raise JournalSchemaError(
            reason=_("the %s table does not exist") % table)
    state_type = next((col['type'] for col in columns
                       if col['name'] == 'state'), None)
    if state_type is None:
        raise JournalSchemaError(
            reason=_("the %s table has no state column") % table)
    # Recording a row sets its sequence number, so without the column
    # every port and network operation would fail.
    if not any(col['name'] == 'seqnum' for col in columns):
        raise JournalSchemaError(
            reason=_("the %s table has no seqnum column") % table)
    db_states = getattr(state_type, 'enums', None)
    # Some databases, such as SQLite, have no enum type to check.
    if not db_states:
        return
    model_states = sdn_journal_db.SdnJournal.__table__.c.state.type.enums
    missing = [state for state in model_states if state not in db_states]
    if missing:
        raise JournalSchemaError(
            reason=_("the %(table)s.state column does not accept the "
                     "states %(missing)s") % {'table': table,
                                              'missing': ', '.join(missing)})
