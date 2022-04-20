# Copyright 2013 Mellanox Technologies, Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import socket
import sys
import time

from neutron.agent import rpc as agent_rpc
from neutron.agent import securitygroups_rpc as agent_sg_rpc
from neutron.api.rpc.handlers import securitygroups_rpc as sg_rpc
from neutron.common import config as common_config
from neutron_lib.agent import topics
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants
from neutron_lib import context
from neutron_lib.utils import helpers as q_utils
from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging
from oslo_service import loopingcall
import six

from networking_mlnx.plugins.ml2.drivers.mlnx.agent import config  # noqa
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import exceptions
from networking_mlnx.plugins.ml2.drivers.mlnx.agent import utils
from networking_mlnx.plugins.ml2.drivers.mlnx import mech_mlnx

LOG = logging.getLogger(__name__)


class EswitchManager(object):
    def __init__(self, interface_mappings, endpoint, timeout):
        self.utils = utils.EswitchUtils(endpoint, timeout)
        self.interface_mappings = interface_mappings
        self.network_map = {}
        self.utils.define_fabric_mappings(interface_mappings)

    def get_port_id_by_pci(self, pci_slot):
        for network_id, data in six.iteritems(self.network_map):
            for port in data['ports']:
                if port['pci_slot'] == pci_slot:
                    return port['port_id']
        LOG.error("Agent cache inconsistency - port id "
                  "is not stored for %s", pci_slot)
        raise exceptions.MlnxException(err_msg=("Agent cache inconsistency, "
                                                "check logs"))

    def get_vnics(self):
        """Get attached VNICs

        :return: list of (<mac>, <pci_slot>) pairs of attached vnics
        """
        vnics = set()
        for vnic in six.itervalues(self.utils.get_attached_vnics()):
            vnics.add((vnic['mac'], vnic['pci_slot']))
        return vnics

    def vnic_port_exists(self, pci_slot):
        return pci_slot in [
            slot for _, slot in self.get_vnics()]

    def remove_network(self, network_id):
        if network_id in self.network_map:
            del self.network_map[network_id]
        else:
            LOG.debug("Network %s not defined on Agent.", network_id)

    def port_down(self, network_id, physical_network, pci_slot):
        """Sets port to down.

        Check internal network map for port data.
        If port exists set port to Down
        """
        for network_id, data in six.iteritems(self.network_map):
            for port in data['ports']:
                if port['pci_slot'] == pci_slot:
                    self.utils.port_down(physical_network, pci_slot)
                    return
        LOG.info('Network %s is not available on this agent', network_id)

    def port_up(self, network_id, network_type,
                physical_network, seg_id, port_id, port_mac, pci_slot):
        """Sets port to up.

        Update internal network map with port data.
        - Check if vnic defined
        - configure eswitch vport
        - set port to Up
        """
        LOG.debug("Connecting port %s", port_id)

        if network_id not in self.network_map:
            self.provision_network(port_id, port_mac,
                                   network_id, network_type,
                                   physical_network, seg_id)
        net_map = self.network_map[network_id]
        net_map['ports'].append({'port_id': port_id, 'port_mac': port_mac,
                                 'pci_slot': pci_slot})

        if network_type == constants.TYPE_VLAN:
            LOG.info('Binding Segmentation ID %(seg_id)s '
                     'to eSwitch for vNIC mac_address %(mac)s',
                     {'seg_id': seg_id, 'mac': port_mac})
        elif network_type == constants.TYPE_FLAT:
            LOG.info('Binding eSwitch for vNIC mac_address %(mac)s'
                     'to flat network', {'mac': port_mac})
            seg_id = 0

        self.utils.set_port_vlan_id(physical_network,
                                    seg_id,
                                    pci_slot)

        self.utils.port_up(physical_network, pci_slot)

    def port_release(self, pci_slot):
        """Clear port configuration from eSwitch."""
        for network_id, net_data in six.iteritems(self.network_map):
            for port in net_data['ports']:
                if port['pci_slot'] == pci_slot:
                    self.utils.port_release(net_data['physical_network'],
                                            pci_slot)
                    return
        LOG.info('Port with PCI %s is not available on this agent', pci_slot)

    def provision_network(self, port_id, port_mac,
                          network_id, network_type,
                          physical_network, segmentation_id):
        LOG.info("Provisioning network %s", network_id)
        data = {
            'physical_network': physical_network,
            'network_type': network_type,
            'ports': [],
            'vlan_id': segmentation_id}
        self.network_map[network_id] = data


class MlnxEswitchRpcCallbacks(sg_rpc.SecurityGroupAgentRpcCallbackMixin):

    # Set RPC API version to 1.3 by default.
    # history
    #   1.1 Support Security Group RPC
    #   1.2 Support DVR (Distributed Virtual Router) RPC (not supported)
    #   1.3 Added param devices_to_update to security_groups_provider_updated
    #       (works with NoopFirewallDriver)
    target = oslo_messaging.Target(version='1.3')

    def __init__(self, context, agent, sg_agent):
        super(MlnxEswitchRpcCallbacks, self).__init__()
        self.context = context
        self.agent = agent
        self.eswitch = agent.eswitch
        self.sg_agent = sg_agent

    def network_delete(self, context, **kwargs):
        LOG.debug("network_delete received")
        network_id = kwargs.get('network_id')
        if not network_id:
            LOG.warning("Invalid Network ID, cannot remove Network")
        else:
            LOG.debug("Delete network %s", network_id)
            self.eswitch.remove_network(network_id)

    def port_update(self, context, **kwargs):
        port = kwargs.get('port')
        mac = port['mac_address']
        pci_slot = None
        if port.get(portbindings.PROFILE):
            pci_slot = port[portbindings.PROFILE].get('pci_slot')

        if pci_slot:
            self.agent.add_port_update((mac, pci_slot))
            LOG.debug("port_update RPC received for port: %(id)s with MAC "
                      "%(mac)s and PCI slot %(pci_slot)s slot",
                      {'id': port['id'], 'mac': mac, 'pci_slot': pci_slot})
        else:
            LOG.debug("port_update RPC received. No PCI Slot for port %(id)s "
                      "with MAC %(mac)s. skipping",
                      {'id': port['id'], 'mac': mac, 'pci_slot': pci_slot})


class MlnxEswitchNeutronAgent(object):

    def __init__(self, interface_mapping):
        self._polling_interval = cfg.CONF.AGENT.polling_interval
        self._setup_eswitches(interface_mapping)
        configurations = {'interface_mappings': interface_mapping}
        self.conf = cfg.CONF
        self.agent_state = {
            'binary': 'neutron-mlnx-agent',
            'host': self.conf.host,
            'topic': constants.L2_AGENT_TOPIC,
            'configurations': configurations,
            'agent_type': mech_mlnx.AGENT_TYPE_MLNX,
            'start_flag': True}
        # Stores port update notifications for processing in main rpc loop
        self.updated_ports = set()
        # A pci to mac dict used to cache port_update information from
        # neutron server.
        self.port_pci_to_mac_cache = dict()
        self.context = context.get_admin_context_without_session()
        self.plugin_rpc = agent_rpc.PluginApi(topics.PLUGIN)
        self.sg_plugin_rpc = sg_rpc.SecurityGroupServerRpcApi(topics.PLUGIN)
        self.sg_agent = agent_sg_rpc.SecurityGroupAgentRpc(self.context,
                self.sg_plugin_rpc)
        self._setup_rpc()

    def _setup_eswitches(self, interface_mapping):
        daemon = cfg.CONF.ESWITCH.daemon_endpoint
        timeout = cfg.CONF.ESWITCH.request_timeout
        self.eswitch = EswitchManager(interface_mapping, daemon, timeout)

    def _report_state(self):
        try:
            devices = len(self.eswitch.get_vnics())
            self.agent_state.get('configurations')['devices'] = devices
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception("Failed reporting state!")

    def _setup_rpc(self):
        self.agent_id = 'mlnx-agent.%s' % socket.gethostname()
        LOG.info("RPC agent_id: %s", self.agent_id)

        self.topic = topics.AGENT
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.REPORTS)
        # RPC network init
        # Handle updates from service
        self.endpoints = [MlnxEswitchRpcCallbacks(self.context, self,
                                                  self.sg_agent)]
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE],
                     [topics.SECURITY_GROUP, topics.UPDATE]]
        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers)

        report_interval = cfg.CONF.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

    def add_port_update(self, port):
        self.updated_ports.add(port)
        # hack to deal with the fact that eswitchd returns invalid macs
        port_mac, port_pci = port
        self.port_pci_to_mac_cache[port_pci] = port_mac

    def fix_eswitchd_vnic_macs(self, eswitchd_ports, neutron_updated_ports):
        # NOTE(adrianc): eswitchd service may return ports with invalid
        # macs since, in virualization use-case the mac will only be
        # visible in the HV only after the VF was binded to the NIC driver in
        # the VM. while a fix in kernel is required, until it lands, "fix" the
        # mac by taking it from cached port info form neutron server
        # for matching entires. Now, the reason we need the MAC to begin with
        # is because Neutron server requires either MAC or Port ID to fetch
        # details in get_devices_details_list RPC call. once we are able to
        # fetch port details from server via PCI slot + host_id we can remove
        # MAC address alltogether from the agent.
        invalid_macs = ('00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff')
        fixed_ports = set()
        for eswp in eswitchd_ports:
            mac, pci_slot = eswp
            if mac in invalid_macs:
                # NOTE(adrianc) Update mac from cache. or skip entry if mac
                # is not cached. We rely on the fact that a port update later
                # down the road will reveal to us neutron's mac address of
                # this port.
                if pci_slot in neutron_updated_ports:
                    updated_mac = neutron_updated_ports[pci_slot]
                    fixed_ports.add((updated_mac, pci_slot))
            else:
                fixed_ports.add(eswp)
        LOG.debug("Eswitchd ports: %s", eswitchd_ports)
        LOG.debug("Updated Eswitchd ports: %s", fixed_ports)
        return fixed_ports

    def scan_ports(self, previous, sync):
        # NOTE(adrianc): Save self.updated_ports and clear it for next
        # iteration. This is threadsafe as greenthreads should not yield
        # between the two operations. We should really consider relying
        # less on greenthread black magic.
        updated_ports = self.updated_ports
        self.updated_ports = set()
        cur_ports = self.fix_eswitchd_vnic_macs(self.eswitch.get_vnics(),
                                                self.port_pci_to_mac_cache)
        port_info = {'current': cur_ports}
        if sync:
            # Either it's the first iteration or previous iteration had
            # problems.
            port_info['added'] = cur_ports
            port_info['removed'] = ((previous['removed'] |
                                    previous['current']) - cur_ports)
            port_info['updated'] = ((previous['updated'] | updated_ports) &
                                    cur_ports)
        else:
            # Shouldn't process updates for not existing ports
            port_info['added'] = cur_ports - previous['current']
            port_info['removed'] = previous['current'] - cur_ports
            port_info['updated'] = updated_ports & cur_ports
        return port_info

    def process_network_ports(self, port_info):
        resync_a = False
        resync_b = False
        device_added_updated = port_info['added'] | port_info['updated']

        if device_added_updated:
            resync_a = self.treat_devices_added_or_updated(
                device_added_updated)
        if port_info['removed']:
            resync_b = self.treat_devices_removed(port_info['removed'])
        # If one of the above opertaions fails => resync with plugin
        return (resync_a | resync_b)

    def treat_vif_port(self, port_id, port_mac, port_pci_slot,
                       network_id, network_type,
                       physical_network, segmentation_id,
                       admin_state_up):
        if self.eswitch.vnic_port_exists(port_pci_slot):
            if admin_state_up:
                self.eswitch.port_up(network_id,
                                     network_type,
                                     physical_network,
                                     segmentation_id,
                                     port_id,
                                     port_mac,
                                     port_pci_slot)
            else:
                self.eswitch.port_down(
                    network_id, physical_network, port_pci_slot)
        else:
            LOG.debug("No port %s defined on agent.", port_id)

    def treat_devices_added_or_updated(self, devices):
        try:
            macs = [mac for mac, _ in devices]
            devs_details_list = self.plugin_rpc.get_devices_details_list(
                self.context,
                macs,
                self.agent_id,
                self.conf.host)
        except Exception as e:
            LOG.debug("Unable to get device details for devices "
                      "%(devices)s: due to %(exc)s",
                      {'devices': devices, 'exc': e})
            # resync is needed
            return True

        for dev_details in devs_details_list:
            device = dev_details['device']
            LOG.info("Adding or updating port with mac %s", device)

            if 'port_id' in dev_details:
                LOG.info("Port %s updated", device)
                LOG.debug("Device details %s", str(dev_details))
                self.treat_vif_port(dev_details['port_id'],
                                    dev_details['device'],
                                    dev_details['profile']['pci_slot'],
                                    dev_details['network_id'],
                                    dev_details['network_type'],
                                    dev_details['physical_network'],
                                    dev_details['segmentation_id'],
                                    dev_details['admin_state_up'])
                LOG.debug("Setting status for %s to UP", device)
                self.plugin_rpc.update_device_up(
                    self.context, device, self.agent_id, self.conf.host)
            else:
                LOG.debug("Setting status for %s to DOWN", device)
                self.plugin_rpc.update_device_down(
                    self.context, device, self.agent_id, self.conf.host)
                LOG.error("Device with mac_address %s not defined "
                          "on Neutron Plugin", device)
        return False

    def treat_devices_removed(self, devices):
        resync = False
        for mac, pci_slot in devices:
            LOG.info("Removing device with mac_address %s", mac)
            try:
                port_id = self.eswitch.get_port_id_by_pci(pci_slot)
                dev_details = self.plugin_rpc.update_device_down(self.context,
                                                                 port_id,
                                                                 self.agent_id,
                                                                 self.conf.host
                                                                 )
            except Exception as e:
                LOG.debug("Removing port failed for device %(mac)s "
                          "due to %(exc)s", {'mac': mac, 'exc': e})
                resync = True
                continue
            if dev_details['exists']:
                LOG.info("Port %s updated.", (mac, pci_slot))
            else:
                LOG.debug("Device %s not defined on plugin", (mac, pci_slot))
            self.eswitch.port_release(pci_slot)
        return resync

    def _port_info_has_changes(self, port_info):
        return (port_info['added'] or
                port_info['removed'] or
                port_info['updated'])

    def run(self):
        LOG.info("eSwitch Agent Started!")
        sync = True
        port_info = {'current': set(),
                     'added': set(),
                     'removed': set(),
                     'updated': set()}
        while True:
            start = time.time()
            try:
                port_info = self.scan_ports(previous=port_info, sync=sync)
            except exceptions.RequestTimeout:
                LOG.exception("Request timeout in agent event loop "
                              "eSwitchD is not responding - exiting...")
                sync = True
                continue
            if sync:
                LOG.info("Agent out of sync with plugin!")
                sync = False
            if self._port_info_has_changes(port_info):
                LOG.debug("Starting to process devices in:%s", port_info)
                try:
                    sync = self.process_network_ports(port_info)
                except Exception:
                    LOG.exception("Error in agent event loop")
                    sync = True
            # sleep till end of polling interval
            elapsed = (time.time() - start)
            if (elapsed < self._polling_interval):
                time.sleep(self._polling_interval - elapsed)
            else:
                LOG.debug("Loop iteration exceeded interval "
                          "(%(polling_interval)s vs. %(elapsed)s)",
                          {'polling_interval': self._polling_interval,
                           'elapsed': elapsed})


def main():
    common_config.register_common_config_options()
    config.config.register_root_helper(cfg.CONF)
    common_config.init(sys.argv[1:])
    common_config.setup_logging()

    try:
        interface_mappings = q_utils.parse_mappings(
            cfg.CONF.ESWITCH.physical_interface_mappings, unique_keys=False)
    except ValueError as e:
        LOG.error("Parsing physical_interface_mappings failed: %s. "
                  "Agent terminated!", e)
        sys.exit(1)
    LOG.info("Interface mappings: %s", interface_mappings)

    try:
        agent = MlnxEswitchNeutronAgent(interface_mappings)
    except Exception:
        LOG.exception("Failed on Agent initialisation: Agent terminated!")
        sys.exit(1)

    # Start everything.
    LOG.info("Agent initialised successfully, now running... ")
    agent.run()
    sys.exit(0)
