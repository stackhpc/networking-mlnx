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

import threading

from neutron.agent.common import utils as n_agent_utils
from neutron.agent.linux import interface as n_interface
from neutron.agent.linux import ip_lib as n_ip_lib
from neutron_lib.utils import helpers
from neutron_lib.utils import runtime
from oslo_config import cfg
from oslo_log import log
from oslo_service import loopingcall
import six

from networking_mlnx.linux.interface_drivers import config
from networking_mlnx.linux.interface_drivers import constants
from networking_mlnx.linux.interface_drivers import network_cache
from networking_mlnx.linux import ip_lib

# Register interface driver Opts when module is loaded
cfg.CONF.register_opts(config.interface_driver_opts)
LOG = log.getLogger(__name__)

DEFAULT_CACHE_CLEANING_INTERVAL = 86400


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


class MultiInterfaceDriver(n_interface.LinuxInterfaceDriver):
    """Driver for invoking interface driver per physnet.

    This driver Delegates the plug/unplug methods
    to other interface drivers per physnet
    according to <physnet: driver> mapping provided in conf file.

    This driver assumes uniqueness in regards to the kind of interface
    created by each delegated driver.
    """

    # Mapping between interface driver and the Type of interface it creates
    # during a call to 'plug()' method.

    # NOTE(adrianc): OVSInterfaceDriver can create either veth or openvswitch
    #                interfaces, however in the mapping we assume that the
    #                latter will be used. It is the responsibility of the
    #                user of this map to check `ovs_use_veth` is false.

    driver_to_interface_kind = {
        n_interface.BridgeInterfaceDriver: constants.INTERFACE_KIND_VETH,
        n_interface.OVSInterfaceDriver: constants.INTERFACE_KIND_OVS,
        n_interface.NullDriver: constants.INTERFACE_KIND_NULL,
        IPoIBInterfaceDriver: constants.INTERFACE_KIND_IPOIB}

    network_cache = None
    _cache_init_lock = threading.RLock()

    def __init__(self, conf, get_networks_callback=None, **kwargs):
        super(MultiInterfaceDriver, self).__init__(
            conf, get_networks_callback=get_networks_callback, **kwargs)
        fields = ['id', constants.PHYSICAL_NETWORK, constants.SEGMENTATION_ID]
        MultiInterfaceDriver._init_network_cache(conf, get_networks_callback,
                                                 fields)
        # Use a cache backed get_networks callback to avoid un-needed RPC calls
        self.drivers = MultiInterfaceDriver.load_interface_driver_mappings(
            conf,
            get_networks_callback=(
                MultiInterfaceDriver._get_networks_from_cache),
            **kwargs)

    @staticmethod
    def _check_drivers(driver_mapping):
        """Check the loaded interface drivers are dealing with a unique
        interface kind.

        :raises SystemExit of 1 in case an inconsistency was found.
        """
        uniq_intf_drivers = []
        driver_types = []
        for driver in six.itervalues(driver_mapping):
            if driver.__class__ not in driver_types:
                driver_types.append(driver.__class__)
                uniq_intf_drivers.append(driver)

        interface_kinds = [driver._interface_kind_ for
                           driver in uniq_intf_drivers]
        if len(interface_kinds) != len(set(interface_kinds)):
            LOG.error("MultiInterfaceDriver cannot operate with interface"
                      "drivers working with the same interface kind.")
            raise SystemExit(1)

    @staticmethod
    def _process_driver_obj(obj):
        """Process a loaded interface driver object.

        :param obj: n_interface.LinuxInterfaceDriver obj.
        :return: An augmented object containing _interface_kind_ attribute that
                 represents the kind of interface a driver creates.
        """

        LOG.debug("Processing driver object of type:%s", obj.__class__)
        if (obj.__class__ is n_interface.OVSInterfaceDriver and
                obj.conf.ovs_use_veth):
            interface_kind = constants.INTERFACE_KIND_VETH
        else:
            interface_kind = (
                MultiInterfaceDriver.driver_to_interface_kind.get(
                    obj.__class__, constants.INTERFACE_KIND_UNKNOWN))

        if hasattr(obj, '_interface_kind_'):
            LOG.error("Attribute '_interface_kind_' defined, unexpected err.")
            raise SystemExit(1)
        obj._interface_kind_ = interface_kind
        return obj

    @staticmethod
    def load_interface_driver_mappings(conf, **kwargs):
        """Load interface drivers for agents like DHCP or L3 agent.

        :param conf: driver configuration object
        :param kwargs: additional keyword arguments
        :raises SystemExit of 1 if drivers cannot be loaded
        """

        try:
            driver_mapping = {}
            mappings = conf.multi_interface_driver_mappings.split(',')
            mappings = helpers.parse_mappings(mappings, False)
            for physnet, intf_driver_name in mappings.items():
                loaded_class = runtime.load_class_by_alias_or_classname(
                    n_agent_utils.INTERFACE_NAMESPACE, intf_driver_name)
                obj = MultiInterfaceDriver._process_driver_obj(
                    loaded_class(conf, **kwargs))
                driver_mapping[physnet] = obj
            MultiInterfaceDriver._check_drivers(driver_mapping)
            return driver_mapping
        except ImportError:
            LOG.error("Error loading interface driver '%s'",
                      conf.interface_driver)
            raise SystemExit(1)

    @classmethod
    def _init_network_cache(cls, conf, get_networks_cb, fields):
        """Initialize a global network cache to be shared between all Multi
        interface driver instances.

        :param conf: configuration object
        :param get_networks_cb: RPC callback to get network information
        :param fields: a list of fields to retrieve per network
        """
        if cls.network_cache:
            # Already initialized
            return
        with cls._cache_init_lock:
            if cls.network_cache:
                # Double checking if already initialized by someone else
                return
            cls.network_cache = network_cache.SafeNetworkCache(
                get_networks_cb, fields)
            # Populate cache
            cls.network_cache.refresh()
            if conf.enable_multi_interface_driver_cache_maintenance:
                remove_stale_entries_loop = (
                    loopingcall.FixedIntervalLoopingCall(
                        cls.network_cache.remove_stale_networks))
                remove_stale_entries_loop.start(
                    DEFAULT_CACHE_CLEANING_INTERVAL,
                    initial_delay=DEFAULT_CACHE_CLEANING_INTERVAL)

    @classmethod
    def _get_networks_from_cache(cls, fields=None, filters=None):
        nets = []
        if cls.network_cache is None:
            LOG.Error("MultiInterfaceDriver network cache was not initialized")
            return nets

        # NOTE(adrianc): since we want to work solely against cls.network_cache
        # it is required to impose some restrictions on filters and fields.
        if filters and list(six.iterkeys(filters)) != ['id']:
            LOG.error("Cache backed get_networks() does not support provided "
                      "filters: %s", filters)
            return nets

        if fields and not set(fields).issubset(set(
                cls.network_cache.network_fields)):
            LOG.warning("Cache backed get_networks() does not contain all "
                        "provided fields: %s", fields)

        if filters is None:
            nets = cls.network_cache.get_all()
        else:
            ids = filters.get('id')
            for id in ids:
                nets.append(cls.network_cache.get(id))

        if fields:
            nets = [{field: net[field] for field in fields
                if field in net.keys()} for net in nets]
            # In case of an empty entry due to missing fields
            # remove from response.
            nets = [net for net in nets if len(net)]

        return nets

    def _get_driver_for_existing_interface(self, device_name, namespace=None):
        dev = n_ip_lib.IPDevice(device_name, namespace)
        kind = dev.link.link_kind
        for driver in six.itervalues(self.drivers):
            if driver._interface_kind_ == kind:
                return driver
        LOG.error("No interface driver found for interface %s of kind %s",
                  device_name, kind)
        return None

    def plug_new(self, network_id, port_id, device_name, mac_address,
                 bridge=None, namespace=None, prefix=None, mtu=None):
        """Plugin the interface."""
        network = MultiInterfaceDriver.network_cache.get(network_id)
        physnet = network.get(constants.PHYSICAL_NETWORK)
        if physnet is None:
            # Tunnels(e.g vxlan, gre) map to the 'nil' interface driver.
            physnet = 'nil'
        try:
            driver = self.drivers[physnet]
            driver.plug_new(network_id, port_id, device_name, mac_address,
                            bridge, namespace, prefix, mtu)
        except KeyError:
            LOG.error("Interface driver not found for physnet: %s", physnet)

    def unplug(self, device_name, bridge=None, namespace=None, prefix=None):
        """Unplug the interface."""
        if not n_ip_lib.device_exists(device_name, namespace):
            # Nothing to unplug.
            return

        driver = self._get_driver_for_existing_interface(device_name,
                                                         namespace)
        if driver:
            driver.unplug(device_name, bridge, namespace, prefix)
        else:
            LOG.error("Failed to unplug interface %s, did not find matching "
                      "interface driver to unplug interface.", device_name)

    def set_mtu(self, device_name, mtu, namespace=None, prefix=None):
        """Set interface MTU"""
        driver = self._get_driver_for_existing_interface(device_name,
                                                         namespace)
        if driver:
            driver.set_mtu(device_name, mtu, namespace, prefix)
        else:
            LOG.error("Failed to set MTU for %s, did not find matching "
                      "interface driver for interface.", device_name)
