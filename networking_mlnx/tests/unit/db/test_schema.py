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

import unittest.mock as mock

from neutron.tests.unit import testlib_api
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy import exc as sa_exc

from networking_mlnx.db import schema
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const

ALL_STATES = (sdn_const.PENDING, sdn_const.FAILED, sdn_const.PROCESSING,
              sdn_const.MONITORING, sdn_const.COMPLETED)


class CheckJournalStatesTestCase(testlib_api.SqlTestCaseLight):

    def _check_with_db_states(self, states):
        # The type that reflection returns for a MySQL or MariaDB enum.
        columns = [{'name': 'id', 'type': sa.String(36)},
                   {'name': 'state', 'type': mysql.ENUM(*states)},
                   {'name': 'seqnum', 'type': sa.BigInteger()}]
        with mock.patch.object(sa, 'inspect') as inspect:
            inspect.return_value.get_columns.return_value = columns
            schema.check_journal_states()
        inspect.return_value.get_columns.assert_called_once_with(
            'sdn_journal')

    def test_all_states(self):
        self._check_with_db_states(ALL_STATES)

    def test_missing_state(self):
        # The schema before the migration that added the monitoring state.
        states = [s for s in ALL_STATES if s != sdn_const.MONITORING]
        e = self.assertRaises(schema.JournalSchemaError,
                              self._check_with_db_states, states)
        self.assertIn('the sdn_journal.state column does not accept the '
                      'states monitoring', str(e))
        self.assertIn('neutron-db-manage --subproject networking-mlnx '
                      'upgrade head', str(e))

    def test_missing_table(self):
        with mock.patch.object(sa, 'inspect') as inspect:
            inspect.return_value.get_columns.side_effect = (
                sa_exc.NoSuchTableError('sdn_journal'))
            e = self.assertRaises(schema.JournalSchemaError,
                                  schema.check_journal_states)
        self.assertIn('the sdn_journal table does not exist', str(e))
        self.assertIn('upgrade head', str(e))

    def test_missing_state_column(self):
        columns = [{'name': 'id', 'type': sa.String(36)}]
        with mock.patch.object(sa, 'inspect') as inspect:
            inspect.return_value.get_columns.return_value = columns
            e = self.assertRaises(schema.JournalSchemaError,
                                  schema.check_journal_states)
        self.assertIn('the sdn_journal table has no state column', str(e))

    def test_missing_seqnum_column(self):
        # The schema before the migration that added the sequence numbers.
        columns = [{'name': 'id', 'type': sa.String(36)},
                   {'name': 'state', 'type': mysql.ENUM(*ALL_STATES)}]
        with mock.patch.object(sa, 'inspect') as inspect:
            inspect.return_value.get_columns.return_value = columns
            e = self.assertRaises(schema.JournalSchemaError,
                                  schema.check_journal_states)
        self.assertIn('the sdn_journal table has no seqnum column', str(e))

    def test_database_without_enums(self):
        # The unit test database is SQLite, which has no enum type.
        schema.check_journal_states()
