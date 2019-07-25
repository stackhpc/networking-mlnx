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

from oslo_log import log as logging

from neutron.agent.linux import ip_lib as n_ip_lib
from neutron.privileged.agent.linux import ip_lib as n_privileged

from networking_mlnx.linux import constants


LOG = logging.getLogger(__name__)


class IPoIBWrapper(n_ip_lib.IPWrapper):

    def __init__(self, *args, **kwargs):
        super(IPoIBWrapper, self).__init__(*args, **kwargs)

    def _segmentation_id_to_pkey(self, segmentation_id):
        # NOTE(adrianc): use the default pkey (7fff) in case:
        # 1. network is flat (segmentation_id is None)
        # 2. segmentation_id is 0
        # PKEY length is 15 bits.
        if segmentation_id is None or segmentation_id == 0:
            return constants.DEFAULT_PKEY
        return int(segmentation_id)

    def add_ipoib(self, name, src_dev, segmentation_id=None):
        LOG.debug("Adding IPoIB device: name:%s, src_dev:%s, "
                  "segmentation_id:%s", name, src_dev, segmentation_id)
        pkey = self._segmentation_id_to_pkey(segmentation_id)
        # NOTE(adrianc): ipoib child interface needs to be created in the
        # same namespace as its root device (i.e the physical interface).
        # Create in default namespace and then move.
        n_privileged.create_interface(name,
                                     None,
                                     "ipoib",
                                     physical_interface=src_dev,
                                     pkey=pkey)
        n_privileged.set_link_attribute(
            name, None, net_ns_fd=self.namespace)
        return n_ip_lib.IPDevice(name, namespace=self.namespace)

    def del_ipoib(self, name):
        LOG.debug("Deleting IPoIB device: name:%s", name)
        n_privileged.delete_interface(name, self.namespace)
