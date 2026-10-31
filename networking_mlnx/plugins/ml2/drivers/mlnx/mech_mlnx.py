# Copyright (c) 2014 OpenStack Foundation
# All Rights Reserved.
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

from neutron.db import models_v2
from neutron.plugins.ml2.drivers import mech_agent
from neutron.plugins.ml2 import models as ml2_models
from neutron_lib.api.definitions import extra_dhcp_opt as edo_ext
from neutron_lib.api.definitions import port
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as p_constants
from neutron_lib.db import api as db_api
from neutron_lib.plugins import directory
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg
from oslo_log import log as logging

from networking_mlnx.plugins.ml2.drivers.mlnx import config
from networking_mlnx.plugins.ml2.drivers.sdn import config as sdn_config
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const

LOG = logging.getLogger(__name__)
cfg.CONF.register_opts(config.mlnx_opts, "mlnx")
cfg.CONF.register_opts(sdn_config.sdn_opts, sdn_const.GROUP_OPT)
SDN_MECHANISM_DRIVER = 'mlnx_sdn_assist'

AGENT_TYPE_MLNX = 'Mellanox plugin agent'
VIF_TYPE_IB_HOSTDEV = 'ib_hostdev'
LEGACY_CLIENT_ID_PREFIX = 'ff:00:00:00:00:00:02:00:00:02:c9:00:'
HARDWARE_CLIENT_ID_PREFIX = '20:'


class MlnxMechanismDriver(mech_agent.SimpleAgentMechanismDriverBase):
    """Attach to networks using Mellanox eSwitch L2 agent.

    The MellanoxMechanismDriver integrates the ml2 plugin with the
    Mellanox eswitch L2 agent. Port binding with this driver requires the
    Mellanox eswitch  agent to be running on the port's host, and that agent
    to have connectivity to at least one segment of the port's
    network.
    """

    def __init__(self):
        super(MlnxMechanismDriver, self).__init__(
            agent_type=AGENT_TYPE_MLNX,
            vif_type=VIF_TYPE_IB_HOSTDEV,
            vif_details={portbindings.CAP_PORT_FILTER: False},
            supported_vnic_types=[portbindings.VNIC_DIRECT])

    def get_allowed_network_types(self, agent=None):
        return [p_constants.TYPE_LOCAL, p_constants.TYPE_FLAT,
                p_constants.TYPE_VLAN]

    def get_mappings(self, agent):
        return agent['configurations'].get('interface_mappings', {})

    def try_to_bind_segment_for_agent(self, context, segment, agent):
        if not self.check_segment_for_agent(segment, agent):
            return False
        if not self._is_pkey_enforced(context, segment):
            LOG.error("Not binding port %(port)s to segment %(segment)s: "
                      "nothing adds ports on physical network %(physnet)s "
                      "to their partition key. Enable the %(sdn)s mechanism "
                      "driver for it, or set [mlnx] allow_unenforced_pkeys.",
                      {'port': context.current['id'],
                       'segment': segment[api.ID],
                       'physnet': segment[api.PHYSICAL_NETWORK],
                       'sdn': SDN_MECHANISM_DRIVER})
            return False
        other_port_id = self._get_port_with_same_guid(context)
        if other_port_id:
            LOG.error("Not binding port %(port)s: port %(other)s has the "
                      "same MAC address, so its virtual function would have "
                      "the same GUID, which would be in both ports' "
                      "partition keys.",
                      {'port': context.current['id'], 'other': other_port_id})
            return False
        if (segment[api.NETWORK_TYPE] in
                (p_constants.TYPE_FLAT, p_constants.TYPE_VLAN)):
            self.vif_details['physical_network'] = segment[
                'physical_network']
        context.set_binding(segment[api.ID],
                            self.vif_type,
                            self.vif_details)
        return True

    @staticmethod
    def _is_pkey_enforced(context, segment):
        """Check if something adds ports on the segment to its pkey

        Ports on flat segments use the default partition key.
        """
        if segment[api.NETWORK_TYPE] != p_constants.TYPE_VLAN:
            return True
        if cfg.CONF.mlnx.allow_unenforced_pkeys:
            return True
        if (SDN_MECHANISM_DRIVER not in cfg.CONF.ml2.mechanism_drivers or
                not cfg.CONF.sdn.sync_enabled):
            return False
        # mlnx_sdn_assist only sends the ports of networks whose segments'
        # physical networks it syncs, all of them.
        physnets = cfg.CONF.sdn.physical_networks
        if sdn_const.ANY in physnets:
            return True
        return all(network_segment.get(api.PHYSICAL_NETWORK) in physnets
                   for network_segment in context.network.network_segments)

    @staticmethod
    def _get_port_with_same_guid(context):
        """Return the ID of another bound port with the same MAC address

        A virtual function's GUID is derived from its port's MAC address,
        which is only unique within a network.
        """
        port = context.current
        plugin_context = context.plugin_context
        with db_api.CONTEXT_READER.using(plugin_context):
            other = plugin_context.session.query(models_v2.Port.id).join(
                ml2_models.PortBinding,
                ml2_models.PortBinding.port_id == models_v2.Port.id).filter(
                models_v2.Port.mac_address == port['mac_address'],
                models_v2.Port.id != port['id'],
                ml2_models.PortBinding.vif_type == VIF_TYPE_IB_HOSTDEV).first()
        return other and other.id

    def _gen_client_id(self, port):
        if cfg.CONF.mlnx.client_id_hardware:
            return self._gen_client_id_with_prefix(port,
                HARDWARE_CLIENT_ID_PREFIX)
        else:
            return self._gen_client_id_with_prefix(port,
                LEGACY_CLIENT_ID_PREFIX)

    def _gen_client_id_with_prefix(self, port, prefix):
        _MIDDLE = ':00:00:'
        mac_address = port["mac_address"]
        mac_first = mac_address[:8]
        mac_last = mac_address[9:]
        client_id = ''.join([prefix, mac_first, _MIDDLE, mac_last])
        return client_id

    def _gen_client_id_opt(self, port):
        client_id = self._gen_client_id(port)
        return [{"opt_name": edo_ext.DHCP_OPT_CLIENT_ID,
                 "opt_value": client_id,
                 "ip_version": 4}]

    def _is_gen_client_id(self, port, client_id):
        # Accept either prefix, in case client_id_hardware has changed since
        # the option was set.
        return client_id in (
            self._gen_client_id_with_prefix(port, HARDWARE_CLIENT_ID_PREFIX),
            self._gen_client_id_with_prefix(port, LEGACY_CLIENT_ID_PREFIX))

    def _gen_none_client_id_opt(self, port):
        updated_extra_dhcp_opts = []
        for opt in port["extra_dhcp_opts"]:
            # Keep any other options, including a client-id set by the user.
            if (opt["opt_name"] == edo_ext.DHCP_OPT_CLIENT_ID and
                    self._is_gen_client_id(port, opt["opt_value"])):
                opt["opt_value"] = None
            updated_extra_dhcp_opts.append(opt)
        return updated_extra_dhcp_opts

    def _can_bind_network(self, context):
        # update_port_precommit is called for every port, so check that a
        # live Mellanox agent on the port's host can bind one of its
        # network's segments.
        agents = [agent for agent in context.host_agents(self.agent_type)
                  if agent['alive']]
        return any(self.check_segment_for_agent(segment, agent)
                   for segment in context.network.network_segments
                   for agent in agents)

    def _process_port_info(self, context):
        original_port = context.original
        updated_port = context.current
        original_host_id = original_port.get("binding:host_id")
        current_host_id = updated_port.get("binding:host_id")
        # in case migration did not take place or delete port
        if original_host_id == current_host_id or current_host_id is None:
            return
        plugin = directory.get_plugin()
        if not updated_port["device_owner"]:
            extra_dhcp_opts = self._gen_none_client_id_opt(updated_port)
            if all(opt["opt_value"] is not None for opt in extra_dhcp_opts):
                return
            updated_port["extra_dhcp_opts"] = extra_dhcp_opts
            plugin._update_extra_dhcp_opts_on_port(
                context._plugin_context,
                updated_port["id"],
                {port.RESOURCE_NAME: updated_port})
        # Normal ports are included because the SDN mechanism driver can
        # bind them to InfiniBand physical networks (bind_normal_ports).
        elif ("compute" in updated_port["device_owner"] and
                updated_port["binding:vnic_type"] in ("direct", "normal") and
                self._can_bind_network(context)):
            client_id = self._gen_client_id(updated_port)
            other_opts = []
            for opt in updated_port.get("extra_dhcp_opts") or []:
                if (opt["opt_name"] != edo_ext.DHCP_OPT_CLIENT_ID or
                        opt.get("ip_version", 4) != 4):
                    other_opts.append(opt)
                # Keep a client-id set by the user, or one that is already
                # up to date.
                elif (opt["opt_value"] == client_id or
                        not self._is_gen_client_id(updated_port,
                                                   opt["opt_value"])):
                    return
            # Keep the port's other options, so that later drivers and the
            # API response see them.
            updated_port["extra_dhcp_opts"] = (
                other_opts + self._gen_client_id_opt(updated_port))
            plugin._update_extra_dhcp_opts_on_port(
                context._plugin_context,
                updated_port["id"],
                {port.RESOURCE_NAME: updated_port})

    def update_port_precommit(self, context):
        # The base class blocks the port from going ACTIVE until the agent
        # reports that it has configured the VF.
        super(MlnxMechanismDriver, self).update_port_precommit(context)
        self._process_port_info(context)
