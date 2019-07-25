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

from neutron.agent.linux import interface as n_interface
from neutron.agent.linux import ip_lib as n_ip_lib
from oslo_config import cfg
from oslo_log import log

from networking_mlnx.linux.interface_drivers import config
from networking_mlnx.linux.interface_drivers import constants
from networking_mlnx.linux import ip_lib

# Register interface driver Opts when module is loaded
cfg.CONF.register_opts(config.interface_driver_opts)
LOG = log.getLogger(__name__)


class IPoIBInterfaceDriver(n_interface.LinuxInterfaceDriver):
    """Driver for creating ipoib interfaces."""

    def __init__(self, conf, get_networks_callback=None, **kwargs):
        super(IPoIBInterfaceDriver, self).__init__(
            conf, get_networks_callback=get_networks_callback, **kwargs)
        self.get_networks = get_networks_callback
        self.root_dev = conf.ipoib_physical_interface
        if not n_ip_lib.device_exists(self.root_dev):
            LOG.error("IPoIB root device %s does not exist.", self.root_dev)

    def plug_new(self, network_id, port_id, device_name, mac_address,
                 bridge=None, namespace=None, prefix=None, mtu=None):
        """Plugin the interface."""
        ip = ip_lib.IPoIBWrapper(namespace=namespace)
        try:
            net = self.get_networks(filters={"id": [network_id]},
                                    fields=[constants.SEGMENTATION_ID])[0]
            segmentation_id = net.get(constants.SEGMENTATION_ID)
            dev = ip.add_ipoib(device_name, self.root_dev, segmentation_id)
            dev.link.set_up()
        except RuntimeError as e:
            LOG.error("Failed plugging interface '%s' - %s",
                      device_name, str(e))

    def unplug(self, device_name, bridge=None, namespace=None, prefix=None):
        """Unplug the interface."""
        ip = ip_lib.IPoIBWrapper(namespace=namespace)
        try:
            ip.del_ipoib(device_name)
        except RuntimeError as e:
            LOG.error("Failed unplugging interface '%s' - %s",
                      device_name, str(e))
