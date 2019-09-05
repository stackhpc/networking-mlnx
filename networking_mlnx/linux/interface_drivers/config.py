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

from oslo_config import cfg

from networking_mlnx._i18n import _


interface_driver_opts = [
    cfg.StrOpt('ipoib_physical_interface',
           default="ib0",
           help=_("Name of the IPoIB root device to use with"
                  "ipoib interface driver.")),
    cfg.StrOpt('multi_interface_driver_mappings',
               default=None,
               help=_("A per physnet interface driver mapping used by "
                      "multidriver interface driver to manage the virtual "
                      "interface per physnet. a virtual network e.g vxlan "
                      "will map to the 'nil' physnet.")),
    cfg.BoolOpt('enable_multi_interface_driver_cache_maintenance',
                default=True,
                help=_("Enable periodic job to perform maintenance to the"
                       "embedded network cache for multi interface driver. "
                       "Set to true if a multi interface driver instance will "
                       "be active for an extended amount of time."))
]
