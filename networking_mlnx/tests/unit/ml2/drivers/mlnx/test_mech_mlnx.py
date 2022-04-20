# Copyright (c) 2014 OpenStack Foundation
# All Rights Reserved.
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

import mock
from neutron.tests.unit.plugins.ml2 import _test_mech_agent as base
from neutron.tests.unit.plugins.ml2 import test_plugin
from neutron_lib.api.definitions import portbindings
from neutron_lib import context
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg
from oslo_config import fixture as fixture_config
from oslo_utils import uuidutils

from networking_mlnx.plugins.ml2.drivers.mlnx import config
from networking_mlnx.plugins.ml2.drivers.mlnx import mech_mlnx

cfg.CONF.import_group("mlnx",
                      'networking_mlnx.plugins.ml2.drivers.mlnx')


class MlnxMechanismBaseTestCase(base.AgentMechanismBaseTestCase):
    VIF_TYPE = mech_mlnx.VIF_TYPE_IB_HOSTDEV
    CAP_PORT_FILTER = False
    AGENT_TYPE = mech_mlnx.AGENT_TYPE_MLNX
    VNIC_TYPE = portbindings.VNIC_DIRECT

    GOOD_MAPPINGS = {'fake_physical_network': 'fake_bridge'}
    GOOD_CONFIGS = {'interface_mappings': GOOD_MAPPINGS}

    BAD_MAPPINGS = {'wrong_physical_network': 'wrong_bridge'}
    BAD_CONFIGS = {'interface_mappings': BAD_MAPPINGS}

    AGENTS = [{'alive': True,
               'configurations': GOOD_CONFIGS,
               'host': 'host',
               'agent_type': AGENT_TYPE}]
    AGENTS_DEAD = [{'alive': False,
                    'configurations': GOOD_CONFIGS,
                    'host': 'dead_host',
                    'agent_type': AGENT_TYPE}]
    AGENTS_BAD = [{'alive': False,
                   'configurations': GOOD_CONFIGS,
                   'host': 'bad_host_1',
                   'agent_type': AGENT_TYPE},
                  {'alive': True,
                   'configurations': BAD_CONFIGS,
                   'host': 'bad_host_2',
                   'agent_type': AGENT_TYPE}]

    def setUp(self):
        super(MlnxMechanismBaseTestCase, self).setUp()
        self.driver = mech_mlnx.MlnxMechanismDriver()
        self.driver.initialize()


class MlnxMechanismGenericTestCase(MlnxMechanismBaseTestCase,
                                   base.AgentMechanismGenericTestCase):
    pass


class MlnxMechanismLocalTestCase(MlnxMechanismBaseTestCase,
                                 base.AgentMechanismLocalTestCase):
    pass


class MlnxMechanismFlatTestCase(MlnxMechanismBaseTestCase,
                                base.AgentMechanismFlatTestCase):
    pass


class MlnxMechanismVlanTestCase(MlnxMechanismBaseTestCase,
                                base.AgentMechanismVlanTestCase):
    pass


class MlnxMechanismVifDetailsTestCase(MlnxMechanismBaseTestCase):
    def setUp(self):
        super(MlnxMechanismVifDetailsTestCase, self).setUp()

    def test_vif_details_contains_physical_net(self):
        VLAN_SEGMENTS = [{api.ID: 'vlan_segment_id',
                          api.NETWORK_TYPE: 'vlan',
                          api.PHYSICAL_NETWORK: 'fake_physical_network',
                          api.SEGMENTATION_ID: 1234}]

        _context = base.FakePortContext(self.AGENT_TYPE,
                                        self.AGENTS,
                                        VLAN_SEGMENTS,
                                        portbindings.VNIC_DIRECT)
        segment = VLAN_SEGMENTS[0]
        agent = self.AGENTS[0]
        self.driver.try_to_bind_segment_for_agent(_context, segment, agent)
        set({"physical_network": "fake_physical_network"}).issubset(
            set(_context._bound_vif_details.items()))


class FakeContext(base.FakePortContext):
    def __init__(self, agent_type, agents, segments,
                 vnic_type=portbindings.VNIC_NORMAL,
                 original=None, current=None):
        super(FakeContext, self).__init__(agent_type, agents,
                                          segments, vnic_type)
        self._original = original
        self._current = current
        self._plugin_context = context.Context('', 'test-tenant')

    @property
    def current(self):
        if self._current:
            return self._current
        return super(FakeContext, self).current

    @current.setter
    def set_current(self, value):
        self._current = value

    @property
    def original(self):
        return self._original

    @original.setter
    def set_original(self, value):
        self._original = value


class MlnxMechanismIbPortTestCase(MlnxMechanismBaseTestCase,
                                  test_plugin.Ml2PluginV2TestCase):
    mechanism_drivers = ['mlnx_infiniband']
    expected_client_id_hardware = (mech_mlnx.HARDWARE_CLIENT_ID_PREFIX +
        '01:23:45:00:00:67:89:ab')
    expected_client_id_legacy = (mech_mlnx.LEGACY_CLIENT_ID_PREFIX +
        "01:23:45:00:00:67:89:ab")

    def setUp(self):
        super(MlnxMechanismIbPortTestCase, self).setUp()
        self.conf_fixture = self.useFixture(fixture_config.Config())
        self.conf = self.conf_fixture.conf
        self.conf.register_opts(config.mlnx_opts, "mlnx")

    def _get_context(self):
        VLAN_SEGMENTS = [{api.ID: 'vlan_segment_id',
                          api.NETWORK_TYPE: 'vlan',
                          api.PHYSICAL_NETWORK: 'fake_physical_network',
                          api.SEGMENTATION_ID: 1234}]
        original_context = {'id': uuidutils.generate_uuid(),
                'binding:host_id': None}
        current_context = {'id': uuidutils.generate_uuid(),
                           'binding:host_id': 'host1',
                           'mac_address': '01:23:45:67:89:ab',
                           'binding:vnic_type': portbindings.VNIC_DIRECT,
                           'device_owner': 'compute'}
        return FakeContext(self.AGENT_TYPE,
                           self.AGENTS,
                           VLAN_SEGMENTS,
               portbindings.VNIC_DIRECT,
                           original=original_context,
                           current=current_context)

    def test_precommit_same_host_id_with_client_id_hardware(self):
        self.conf.set_override('client_id_hardware', True, "mlnx")
        _context = self._get_context()
        with mock.patch('neutron_lib.plugins.directory.get_plugin'):
            self.driver.update_port_precommit(_context)
        self.assertIsNotNone(_context.current.get('extra_dhcp_opts'))
        self.assertEqual(self.expected_client_id_hardware,
                         _context.current['extra_dhcp_opts'][0]['opt_value'])

    def test_precommit_same_host_id_with_client_id_legacy(self):
        self.conf.set_override('client_id_hardware', False, "mlnx")
        _context = self._get_context()
        with mock.patch('neutron_lib.plugins.directory.get_plugin'):
            self.driver.update_port_precommit(_context)
        self.assertIsNotNone(_context.current.get('extra_dhcp_opts'))
        self.assertEqual(self.expected_client_id_legacy,
                         _context.current['extra_dhcp_opts'][0]['opt_value'])

    def test_percommit_migrete_port(self):
        _context = self._get_context()
        _context.current['binding:host_id'] = 'host2'
        with mock.patch('neutron_lib.plugins.directory.get_plugin'):
            self.driver.update_port_precommit(_context)
        self.assertIsNotNone(_context.current.get('extra_dhcp_opts'))
