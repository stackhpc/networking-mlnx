# Copyright 2020 Mellanox Technologies, Ltd
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

from oslo_config import cfg

from networking_mlnx._i18n import _

mlnx_opts = [
    cfg.BoolOpt('client_id_hardware',
                default="True",
                help=_("Generate client-id according to send "
                       "dhcp-client-identifier = hardware")),
    cfg.BoolOpt('allow_unenforced_pkeys',
                default=False,
                help=_("Bind ports on VLAN segments even if the "
                       "mlnx_sdn_assist mechanism driver doesn't sync the "
                       "segment's physical network to an SDN controller, "
                       "which adds ports to their partition key. Only set "
                       "this if the subnet manager's partition configuration "
                       "matches the networks' segmentation IDs, or the "
                       "virtual functions are ConnectX-3 ones, whose "
                       "partition key eswitchd sets.")),
]
