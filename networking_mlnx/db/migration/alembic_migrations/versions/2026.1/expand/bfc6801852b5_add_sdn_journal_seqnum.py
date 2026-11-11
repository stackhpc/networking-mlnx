# Copyright 2026 OpenStack Foundation
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

"""add sdn_journal seqnum

Revision ID: bfc6801852b5
Create Date: 2026-10-31 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bfc6801852b5'
down_revision = '5d5e04ea01d5'


def upgrade():
    op.add_column('sdn_journal',
                  sa.Column('seqnum', sa.BigInteger(), nullable=True))
    op.create_unique_constraint('uniq_sdn_journal0object_uuid0seqnum',
                                'sdn_journal', ['object_uuid', 'seqnum'])
