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

import neutron_lib.api.definitions.provider_net as prov_net_attr

SEGMENTATION_ID = prov_net_attr.SEGMENTATION_ID
PHYSICAL_NETWORK = prov_net_attr.PHYSICAL_NETWORK
ADMIN_STATE_UP = 'admin_state_up'

# Types of interfaces created by various neutron interface drivers
INTERFACE_KIND_UNKNOWN = 'unknown'
INTERFACE_KIND_OVS = 'openvswitch'
INTERFACE_KIND_VETH = 'veth'
INTERFACE_KIND_NULL = 'null'
INTERFACE_KIND_IPOIB = 'ipoib'
