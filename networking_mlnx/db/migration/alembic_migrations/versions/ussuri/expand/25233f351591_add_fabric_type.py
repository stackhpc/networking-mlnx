# Copyright 2019 Mellanox Technologies, Ltd
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

"""add fabric_type

Revision ID: 25233f351591
Create Date: 2019-11-05 12:19:06.653541

"""

from alembic import op
import sqlalchemy as sa

from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const


# revision identifiers, used by Alembic.
revision = '25233f351591'
down_revision = '5d5e04ea01d5'


def upgrade():
    op.add_column('sdn_journal', sa.Column(
        'fabric_type', sa.Enum(sdn_const.FABRIC_ETH, sdn_const.FABRIC_IB),
        default=sdn_const.FABRIC_ETH))
