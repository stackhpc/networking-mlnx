# Copyright 2015 Mellanox Technologies, Ltd
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
import datetime
import unittest.mock as mock

from neutron.db import provisioning_blocks
from neutron.objects import network as network_obj
from neutron.plugins.ml2 import models as ml2_models
from neutron.plugins.ml2 import plugin
from neutron.tests.unit.plugins.ml2 import test_plugin
from neutron.tests.unit import testlib_api
from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib.callbacks import exceptions
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants
from neutron_lib import context as nl_context
from neutron_lib.db import api as db_api
from neutron_lib import exceptions as n_exc
from oslo_config import cfg
from oslo_config import fixture as fixture_config
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests

from networking_mlnx.db import db
from networking_mlnx.db import schema
from networking_mlnx.journal import cleanup
from networking_mlnx.journal import journal
from networking_mlnx.plugins.ml2.drivers.mlnx import mech_mlnx
from networking_mlnx.plugins.ml2.drivers.sdn import client
from networking_mlnx.plugins.ml2.drivers.sdn import config
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_excpt
from networking_mlnx.plugins.ml2.drivers.sdn import sdn_mech_driver
from networking_mlnx.plugins.ml2.drivers.sdn import utils as sdn_utils

PLUGIN_NAME = 'neutron.plugins.ml2.plugin.Ml2Plugin'
SEG_ID = 4
DEVICE_OWNER_COMPUTE = 'compute:None'
CLIENT_ID = 'ff:00:00:00:00:00:02:00:00:02:c9:00:b8:3f:d2:03:00:7b:74:9a'
MECHANISM_DRIVER_NAME = 'mlnx_sdn_assist'


cfg.CONF.import_group(sdn_const.GROUP_OPT,
                      'networking_mlnx.plugins.ml2.drivers.sdn')


class SdnConfigBase(test_plugin.Ml2PluginV2TestCase):

    def setUp(self):
        super(SdnConfigBase, self).setUp()
        self.conf_fixture = self.useFixture(fixture_config.Config())
        self.conf = self.conf_fixture.conf
        self.conf.register_opts(config.sdn_opts, sdn_const.GROUP_OPT)
        self.conf.set_override('mechanism_drivers',
                              ['logger', MECHANISM_DRIVER_NAME],
                              'ml2')
        self.conf.set_override('url', 'http://127.0.0.1/ufmRestV3',
                               sdn_const.GROUP_OPT)
        self.conf.set_override('token', 'abcdef', sdn_const.GROUP_OPT)
        self.conf.set_override('cert_verify', False, sdn_const.GROUP_OPT)


class SdnTestCase(SdnConfigBase):

    def setUp(self):
        super(SdnTestCase, self).setUp()
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        mock.patch.object(journal.SdnJournalThread,
                          'start_sync_thread').start()
        self.mock_request = mock.patch.object(client.SdnRestClient,
                                              'request').start()
        self.mock_request.side_effect = self.check_request

    def check_request(self, method, urlpath, obj):
        self.assertFalse(urlpath.startswith("http://"))


class SdnMechanismConfigTests(testlib_api.SqlTestCase):

    def setUp(self):
        super(SdnMechanismConfigTests, self).setUp()
        # Creating the plugin initialises the driver, which would otherwise
        # start a sync thread and timer that outlive the test and sync the
        # journal in the middle of later tests.
        mock.patch.object(journal.SdnJournalThread,
                          'start_sync_thread').start()
        mock.patch.object(journal.SdnJournalThread,
                          '_start_sync_timer').start()
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.conf_fixture = self.useFixture(fixture_config.Config())
        self.conf = self.conf_fixture.conf
        self.conf.register_opts(config.sdn_opts, sdn_const.GROUP_OPT)
        self._set_config()

    def _set_config(self, url='http://127.0.0.1/ufmRestV3',
                    token='abcdef',
                    cert_verify=False,
                    sync_enabled=True,
                    **additional_config):
        self.conf.set_override('mechanism_drivers',
                               ['logger', MECHANISM_DRIVER_NAME],
                               'ml2')
        self.conf.set_override('url', url, sdn_const.GROUP_OPT)
        self.conf.set_override('token', token, sdn_const.GROUP_OPT)
        self.conf.set_override('cert_verify', False, sdn_const.GROUP_OPT)

        self.conf.set_override('sync_enabled', sync_enabled,
                               sdn_const.GROUP_OPT)
        for k, v in additional_config.items():
            self.conf.set_override(k, v, sdn_const.GROUP_OPT)

    def _test_missing_config(self, **kwargs):
        self._set_config(**kwargs)
        self.assertRaises(cfg.RequiredOptError,
                          plugin.Ml2Plugin)

    def test_valid_config(self):
        self._set_config()
        plugin.Ml2Plugin()

    def test_missing_url_raises_exception(self):
        self._test_missing_config(url=None)

    def test_missing_token_raises_exception(self):
        self._test_missing_config(token=None)

    def test_missing_config_ok_when_disabled(self):
        self._set_config(url=None, token=None,
                         sync_enabled=False)
        plugin.Ml2Plugin()

    @mock.patch.object(schema, 'check_journal_states',
                       side_effect=schema.JournalSchemaError(
                           reason='test'))
    def test_journal_schema_error_raises_exception(self, check):
        self.assertRaises(schema.JournalSchemaError, plugin.Ml2Plugin)
        check.assert_called_once_with()

    @mock.patch.object(schema, 'check_journal_states')
    def test_journal_schema_not_checked_when_disabled(self, check):
        self._set_config(sync_enabled=False)
        plugin.Ml2Plugin()
        check.assert_not_called()

    def test__check_physnet_confs_bind_normal_ports_false_any_physnet(self):
        self._set_config(physical_networks=['*'],
                         bind_normal_ports=False,
                         bind_normal_ports_physnets=["testphys1"])
        # Check should essentially always pass as bind_normal_ports is False
        self.mech._check_physnet_confs()

    def test__check_physnet_confs_bind_normal_ports_false_with_physnet(self):
        self._set_config(physical_networks=['testphys1'],
                         bind_normal_ports=False,
                         bind_normal_ports_physnets=["testphys2"])
        # Check should essentially always pass as bind_normal_ports is False
        self.mech._check_physnet_confs()

    def test__check_physnet_confs_any_physnet(self):
        self._set_config(physical_networks=['*'],
                         bind_normal_ports=True,
                         bind_normal_ports_physnets=["testphys1"])
        self.mech._check_physnet_confs()

    def test__check_physnet_confs_with_physnets(self):
        self._set_config(physical_networks=['testphys1'],
                         bind_normal_ports=True,
                         bind_normal_ports_physnets=["testphys1"])
        self.mech._check_physnet_confs()

    def test__check_physnet_confs_with_physnets_no_subset(self):
        self._set_config(physical_networks=['testphys1', 'testphys2'],
                         bind_normal_ports=True,
                         bind_normal_ports_physnets=["testphys3"])
        self.assertRaises(sdn_excpt.SDNDriverConfError,
                          self.mech._check_physnet_confs)

    class SdnMechanismTestBasicGet(test_plugin.TestMl2BasicGet,
                                   SdnTestCase):
        pass

    class SdnMechanismTestNetworksV2(test_plugin.TestMl2NetworksV2,
                                     SdnTestCase):
        pass

    class SdnMechanismTestPortsV2(test_plugin.TestMl2PortsV2,
                                  SdnTestCase):
        pass


class DataMatcher(object):

    def __init__(self, context, object_type):
        self._data = context.__dict__["_" + object_type.lower()]

    def __eq__(self, data):
        return jsonutils.loads(data) == self._data

    def __repr__(self):
        return jsonutils.dumps(self._data)


class SdnDriverTestCase(SdnConfigBase):

    OPERATION_MAPPING = {
        sdn_const.PUT: 'update',
        sdn_const.DELETE: 'delete',
        sdn_const.POST: 'create',
    }

    def setUp(self):
        super(SdnDriverTestCase, self).setUp()
        self.context = nl_context.get_admin_context()
#        self.db_session = context.session
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mock_sync_thread = mock.patch.object(
            journal.SdnJournalThread, 'start_sync_thread').start()
        self.mech.initialize()
        self.thread = journal.SdnJournalThread()
        self.addCleanup(self._db_cleanup)

    def _get_segments_list(self, seg_id=SEG_ID, net_type=constants.TYPE_VLAN):
        return [{'segmentation_id': seg_id,
                'physical_network': u'physnet1',
                'id': u'c13bba05-eb07-45ba-ace2-765706b2d701',
                'network_type': net_type}]

    def _get_mock_network_operation_context(self):
        current = {"provider:segmentation_id": SEG_ID,
                   'id': 'c13bba05-eb07-45ba-ace2-765706b2d701',
                   'name': 'net1',
                   'provider:network_type': 'vlan',
                   'network_qos_policy': None}
        context = mock.Mock(current=current, _network=current,
                            _segments=self._get_segments_list(),
                            network_segments=self._get_segments_list())
        context._plugin_context = nl_context.get_admin_context()
        return context

    def _get_mock_port_operation_context(self):
        current = {'binding:host_id': 'r-ufm177',
                   'binding:profile': {u'pci_slot': u'0000:02:00.4',
                                       u'physical_network': u'physnet1',
                                       u'pci_vendor_info': u'15b3:1004'},
                   'id': '72c56c48-e9b8-4dcf-b3a7-0813bb3bd839',
                   'binding:vnic_type': 'direct',
                   'device_owner': DEVICE_OWNER_COMPUTE,
                   'mac_address': '12:34:56:78:21:b6',
                   'name': 'port_test1',
                   'network_id': 'c13bba05-eb07-45ba-ace2-765706b2d701',
                   'network_qos_policy': None}
        original = {'binding:host_id': None,
                    'binding:profile': {u'pci_slot': None,
                                        u'physical_network': u'physnet1',
                                        u'pci_vendor_info': u'15b3:1004'},
                    'id': None,
                    'binding:vnic_type': None,
                    'device_owner': DEVICE_OWNER_COMPUTE,
                    'mac_address': None,
                    'name': None,
                    'network_id': 'c13bba05-eb07-45ba-ace2-765706b2d701',
                    'network_qos_policy': None}

        # The port context should have NetwrokContext object that contain
        # the segments list
        network_context = type('NetworkContext', (object,),
                            {"network_segments": self._get_segments_list()})

        context = mock.Mock(current=current, _port=current,
                            original=original,
                            network=network_context,
                            host=current['binding:host_id'],
                            original_host=original['binding:host_id'],
                            vif_type=portbindings.VIF_TYPE_UNBOUND,
                            original_vif_type=portbindings.VIF_TYPE_UNBOUND)
        context._plugin_context = nl_context.get_admin_context()
        return context

    def _get_mock_bind_operation_context(self):
        current = {'binding:host_id': 'r-ufm177',
                   'binding:profile': {u'pci_slot': u'0000:02:00.4',
                                       u'physical_network': u'physnet1',
                                       u'pci_vendor_info': u'15b3:1004'},
                   'id': '72c56c48-e9b8-4dcf-b3a7-0813bb3bd839',
                   'binding:vnic_type': 'direct',
                   'mac_address': '12:34:56:78:21:b6',
                   'name': 'port_test1',
                   'device_owner': DEVICE_OWNER_COMPUTE,
                   'network_id': 'c13bba05-eb07-45ba-ace2-765706b2d701',
                   'network_qos_policy': None}
        # The port context should have NetwrokContext object that contain
        # the segments list
        network_context = type('NetworkContext', (object,),
                            {"network_segments": self._get_segments_list()})
        context = mock.Mock(current=current, _port=current,
                            segments_to_bind=self._get_segments_list(),
                            network=network_context)
        context._plugin_context = nl_context.get_admin_context()
        return context

    def _get_mock_commit_operation_context(self, net_type=constants.TYPE_VLAN):
        # ML2 committing the binding of the port to its host
        context = self._get_mock_port_operation_context()
        context.original = dict(context.current)
        context.original_host = context.host
        context.vif_type = mech_mlnx.VIF_TYPE_IB_HOSTDEV
        context.bottom_bound_segment = self._get_segments_list(
            net_type=net_type)[0]
        return context

    def _get_mock_operation_context(self, object_type):
        getter = getattr(self, '_get_mock_%s_operation_context' %
                         object_type.lower())
        return getter()

    _status_code_msgs = {
        200: '',
        201: '',
        204: '',
        400: '400 Client Error: Bad Request',
        401: '401 Client Error: Unauthorized',
        403: '403 Client Error: Forbidden',
        404: '404 Client Error: Not Found',
        409: '409 Client Error: Conflict',
        501: '501 Server Error: Not Implemented',
        503: '503 Server Error: Service Unavailable',
    }

    def _get_http_request_codes(self):
        for err_code in (requests.codes.ok,
                         requests.codes.created,
                         requests.codes.no_content,
                         requests.codes.bad_request,
                         requests.codes.unauthorized,
                         requests.codes.forbidden,
                         requests.codes.not_found,
                         requests.codes.conflict,
                         requests.codes.not_implemented,
                         requests.codes.service_unavailable):
            yield err_code

    def _db_cleanup(self):
        rows = db.get_all_db_rows(self.context)
        for row in rows:
            db.delete_row(self.context, row=row)

    @classmethod
    def _get_mock_request_response(cls, status_code, job_url):
        response = mock.Mock(status_code=status_code)
        if status_code < 400:
            response.raise_for_status = mock.Mock()
            response.json = mock.Mock(
                side_effect=[job_url, {"Status": "Completed"}])
        else:
            mock.Mock(side_effect=requests.exceptions.HTTPError(
                cls._status_code_msgs[status_code]))

        return response

    def _test_operation(self, method, status_code, expected_calls,
                        *args, **kwargs):
        job_url = 'app/jobs/' + uuidutils.generate_uuid()
        urlpath = sdn_utils.strings_to_url(
            cfg.CONF.sdn.url, job_url)

        request_response = self._get_mock_request_response(
            status_code, job_url)

        if expected_calls == 2 and status_code < 400:
            job_url2 = 'app/jobs/' + uuidutils.generate_uuid()
            request_response.json = mock.Mock(
                side_effect=[job_url, job_url2,
                {"Status": "Completed"}, {"Status": "Completed"}])
        with mock.patch('requests.request',
                        return_value=request_response) as mock_method:

            method(exit_after_run=True)
            job_get_args = mock.call(
                sdn_const.GET, data=None,
                headers={"Authorization": "Basic {0}".format(
                    cfg.CONF.sdn.token), **sdn_const.JSON_HTTP_HEADER},
                url=urlpath, timeout=cfg.CONF.sdn.timeout,
                verify=cfg.CONF.sdn.cert_verify)

            if status_code < 400:
                if expected_calls:
                    operation_args = mock.call(
                        headers={"Authorization": "Basic {0}".format(
                            cfg.CONF.sdn.token),
                            **sdn_const.JSON_HTTP_HEADER},
                        timeout=cfg.CONF.sdn.timeout, *args, **kwargs,
                        verify=cfg.CONF.sdn.cert_verify)
                    if expected_calls == 2:
                        urlpath2 = sdn_utils.strings_to_url(
                            cfg.CONF.sdn.url, job_url2)
                        job_get_args2 = mock.call(
                            sdn_const.GET, data=None,
                            headers={"Authorization": "Basic {0}".format(
                                cfg.CONF.sdn.token),
                                **sdn_const.JSON_HTTP_HEADER},
                            url=urlpath2, timeout=cfg.CONF.sdn.timeout,
                            verify=cfg.CONF.sdn.cert_verify)
                        self.assertEqual(
                            job_get_args, mock_method.mock_calls[2])
                        self.assertEqual(
                            job_get_args2, mock_method.mock_calls[3])
                    else:
                        self.assertEqual(
                            operation_args, mock_method.mock_calls[0])
                        self.assertEqual(
                            job_get_args, mock_method.mock_calls[1])

                # we need to reduce the login call_cout
                self.assertEqual(expected_calls * 2, mock_method.call_count)

    def _call_operation_object(self, operation, object_type):
        if object_type == sdn_const.PORT and operation == sdn_const.POST:
            context = self._get_mock_commit_operation_context()
            method = getattr(self.mech, 'update_port_precommit')
        else:
            context = self._get_mock_operation_context(object_type)
            operation = self.OPERATION_MAPPING[operation]
            object_type = object_type.lower()
            method = getattr(self.mech, '%s_%s_precommit' % (operation,
                                                             object_type))
        method(context)

    def _test_operation_object(self, operation, object_type):
        self._call_operation_object(operation, object_type)

        context = self._get_mock_operation_context(object_type)
        row = db.get_oldest_pending_db_row_with_lock(context._plugin_context)
        self.assertEqual(operation, row['operation'])
        self.assertEqual(object_type, row['object_type'])
        self.assertEqual(context.current['id'], row['object_uuid'])

    def _test_thread_processing(self, operation, object_type,
                                expected_calls=1):
        status_codes = {sdn_const.POST: requests.codes.created,
                        sdn_const.PUT: requests.codes.ok,
                        sdn_const.DELETE: requests.codes.no_content}

        http_request = operation
        status_code = status_codes[operation]

        self._call_operation_object(operation, object_type)

        if object_type == sdn_const.PORT and operation == sdn_const.POST:
            context = self._get_mock_bind_operation_context()
        else:
            context = self._get_mock_operation_context(object_type)

        url_object_type = object_type.replace('_', '-')
        url = '%s/%s/%s' % (cfg.CONF.sdn.url,
                            cfg.CONF.sdn.domain,
                            url_object_type)
        if operation in (sdn_const.PUT, sdn_const.DELETE):
            uuid = context.current['id']
            url = '%s/%s' % (url, uuid)
        kwargs = {'url': url, 'data': DataMatcher(context, object_type)}
        with mock.patch.object(self.thread.event, 'wait',
                               return_value=False):
            self._test_operation(self.thread.run_sync_thread, status_code,
                                 expected_calls, http_request, **kwargs)

    def _test_object_type(self, object_type):
        # Add and process create request.
        self._test_thread_processing(sdn_const.POST, object_type)
        rows = db.get_all_db_rows_by_state(self.context,
                                           sdn_const.COMPLETED)
        self.assertEqual(1, len(rows))

        # Add and process update request. Adds to database.
        self._test_thread_processing(sdn_const.PUT, object_type)
        rows = db.get_all_db_rows_by_state(self.context,
                                           sdn_const.COMPLETED)
        self.assertEqual(2, len(rows))

        # Add and process update request. Adds to database.
        self._test_thread_processing(sdn_const.DELETE, object_type)
        rows = db.get_all_db_rows_by_state(self.context,
                                           sdn_const.COMPLETED)
        self.assertEqual(3, len(rows))

    def _test_object_type_pending_network(self, object_type):
        # Create a network and an object_type on it (creates db rows in
        # pending state).
        self._call_operation_object(sdn_const.POST, sdn_const.NETWORK)
        self._call_operation_object(sdn_const.POST, object_type)

        def request(method, url, **kwargs):
            if method == sdn_const.GET:
                return self._response(requests.codes.ok,
                                      '{"Status": "Completed"}')
            return self._response(requests.codes.accepted, '"app/jobs/1"')

        def sync():
            with self._limit_row_fetches(), \
                    mock.patch('requests.request',
                               side_effect=request) as mock_request:
                self.thread._sync_pending_rows(self.context, False)
                self.thread._sync_progress_rows(self.context)
            return [c.args[0] + ' ' + c.kwargs['url'].rsplit('/', 1)[-1]
                    for c in mock_request.call_args_list]

        # The object_type waits until the controller has created the
        # network, so the first sync only sends the network.
        self.assertEqual(['POST Network', 'GET 1'], sync())
        rows = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        self.assertEqual([object_type], [r.object_type for r in rows])

        url_object_type = object_type.replace('_', '-').title()
        self.assertEqual(['POST ' + url_object_type, 'GET 1'], sync())
        rows = db.get_all_db_rows_by_state(self.context,
                                           sdn_const.COMPLETED)
        self.assertEqual(2, len(rows))

    def _test_object_type_processing_network(self, object_type):
        self._test_object_operation_pending_another_object_operation(
            object_type, sdn_const.POST, sdn_const.NETWORK,
            sdn_const.POST)

    def _test_object_operation_pending_object_operation(
        self, object_type, operation, pending_operation):
        self._test_object_operation_pending_another_object_operation(
            object_type, operation, object_type, pending_operation)

    def _test_object_operation_pending_another_object_operation(
        self, object_type, operation, pending_type, pending_operation,
        pending_state=sdn_const.PROCESSING):
        # Create the object_type (creates db row in pending state).
        self._call_operation_object(pending_operation,
                                    pending_type)

        # Get pending row and mark as processing (or monitoring) so that
        # this row will not be processed by journal thread.
        row = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        db.update_db_row_state(self.context, row[0], pending_state)

        # Create the object_type database row and process.
        # Verify that object request is not processed because the
        # dependent object operation has not been marked as 'completed'.
        self._test_thread_processing(operation,
                                     object_type,
                                     expected_calls=0)

        # Verify that all rows are still in the database.
        rows = db.get_all_db_rows_by_state(self.context, pending_state)
        self.assertEqual(1, len(rows))
        rows = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        self.assertEqual(1, len(rows))

    def _test_parent_delete_pending_child_delete(self, parent, child):
        self._test_object_operation_pending_another_object_operation(
            parent, sdn_const.DELETE, child, sdn_const.DELETE)

    def _test_cleanup_processing_rows(self, last_retried, expected_state):
        # Create a dummy network (creates db row in pending state).
        self._call_operation_object(sdn_const.POST,
                                    sdn_const.NETWORK)

        # Get pending row and mark as processing and update
        # the last_retried time
        row = db.get_all_db_rows_by_state(self.context,
                                          sdn_const.PENDING)[0]
        row.last_retried = last_retried
        db.update_db_row_state(self.context, row, sdn_const.PROCESSING)

        # Test if the cleanup marks this in the desired state
        # based on the last_retried timestamp
        cleanup.JournalCleanup().cleanup_processing_rows(self.context)

        # Verify that the Db row is in the desired state
        rows = db.get_all_db_rows_by_state(self.context, expected_state)
        self.assertEqual(1, len(rows))

    def test_driver(self):
        for operation in (sdn_const.POST, sdn_const.PUT,
                          sdn_const.DELETE):
            for object_type in (sdn_const.NETWORK, sdn_const.PORT):
                self._test_operation_object(operation, object_type)

    def test_network(self):
        self._test_object_type(sdn_const.NETWORK)

    def test_network_update_pending_network_create(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.NETWORK, sdn_const.PUT, sdn_const.POST)

    def test_network_delete_pending_network_create(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.NETWORK, sdn_const.DELETE, sdn_const.POST)

    def test_network_delete_pending_network_update(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.NETWORK, sdn_const.DELETE, sdn_const.PUT)

    def test_network_delete_pending_port_delete(self):
        self._test_parent_delete_pending_child_delete(
            sdn_const.NETWORK, sdn_const.PORT)

    def test_network_delete_monitoring_port_delete(self):
        # UFM rejects deleting a network while its port deletion job runs.
        self._test_object_operation_pending_another_object_operation(
            sdn_const.NETWORK, sdn_const.DELETE, sdn_const.PORT,
            sdn_const.DELETE, pending_state=sdn_const.MONITORING)

    def test_network_delete_monitoring_network_create(self):
        self._test_object_operation_pending_another_object_operation(
            sdn_const.NETWORK, sdn_const.DELETE, sdn_const.NETWORK,
            sdn_const.POST, pending_state=sdn_const.MONITORING)

    def test_port_monitoring_network(self):
        self._test_object_operation_pending_another_object_operation(
            sdn_const.PORT, sdn_const.POST, sdn_const.NETWORK,
            sdn_const.POST, pending_state=sdn_const.MONITORING)

    def test_port_delete_monitoring_port_create(self):
        self._test_object_operation_pending_another_object_operation(
            sdn_const.PORT, sdn_const.DELETE, sdn_const.PORT,
            sdn_const.POST, pending_state=sdn_const.MONITORING)

    def _limit_row_fetches(self, limit=20):
        """Fail, rather than hang, if a sync run never ends."""
        get_row = db.get_oldest_valid_pending_db_row_with_lock
        calls = []

        def get_row_with_limit(*args, **kwargs):
            calls.append(None)
            if len(calls) > limit:
                raise AssertionError('The sync run did not end')
            return get_row(*args, **kwargs)

        return mock.patch.object(db,
                                 'get_oldest_valid_pending_db_row_with_lock',
                                 side_effect=get_row_with_limit)

    @db_api.CONTEXT_WRITER
    def _set_row_time(self, context, row, last_retried):
        row.last_retried = last_retried
        context.session.merge(row)

    def test_sync_pending_rows_skips_waiting_row_with_same_time(self):
        # A ready row with the same last_retried time as a waiting row must
        # still be synced in the same run.
        self._call_operation_object(sdn_const.DELETE, sdn_const.PORT)
        port_row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_state(self.context, port_row, sdn_const.MONITORING)
        # Waits for the port deletion.
        self._call_operation_object(sdn_const.DELETE, sdn_const.NETWORK)
        # Ready: an update of another network.
        context = self._get_mock_network_operation_context()
        context.current['id'] = uuidutils.generate_uuid()
        self.mech.update_network_precommit(context)
        now = datetime.datetime.utcnow().replace(microsecond=0)
        for row in db.get_all_db_rows_by_state(self.context,
                                               sdn_const.PENDING):
            self._set_row_time(self.context, row, now)

        response = self._response(requests.codes.accepted, '"app/jobs/1"')
        with self._limit_row_fetches(), \
                mock.patch('requests.request',
                           return_value=response) as mock_request:
            self.thread._sync_pending_rows(self.context, False)

        self.assertEqual(1, mock_request.call_count)
        self.assertTrue(mock_request.call_args.kwargs['url'].endswith(
            context.current['id']))
        rows = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        self.assertEqual([sdn_const.NETWORK], [r.object_type for r in rows])
        self.assertEqual(sdn_const.DELETE, rows[0].operation)

    def _test_sync_progress_row(self, response):
        self._call_operation_object(sdn_const.POST, sdn_const.NETWORK)
        row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)

        with mock.patch('requests.request', return_value=response):
            self.thread._sync_progress_rows(self.context)

        # The request is sent again, rather than monitored forever, and this
        # counts against retry_count.
        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.PENDING, row.state)
        self.assertEqual(1, row.retry_count)

    def test_sync_progress_rows_unreadable_job(self):
        for text in ('not json', '["Completed"]'):
            self._test_sync_progress_row(
                self._response(requests.codes.ok, text))
            self._db_cleanup()

    def test_sync_progress_rows_job_failed(self):
        self._test_sync_progress_row(
            self._response(requests.codes.ok, '{"Status": "Failed"}'))

    def test_sync_progress_rows_job_not_implemented(self):
        self._test_sync_progress_row(
            self._response(requests.codes.not_implemented))

    def test_sync_pending_rows_without_job_id_completes_row(self):
        for operation in (sdn_const.POST, sdn_const.PUT):
            self._call_operation_object(operation, sdn_const.NETWORK)
            with mock.patch('requests.request',
                            return_value=self._response(requests.codes.ok,
                                                        'null')):
                self.thread._sync_pending_rows(self.context, False)

            row = db.get_all_db_rows(self.context)[0]
            self.assertEqual(sdn_const.COMPLETED, row.state)
            self._db_cleanup()

    def test_sync_pending_rows_delete_without_job_id_is_retried(self):
        # Nothing shows that the deletion was done, so it is sent again.
        for text in ('null', '<html>no job link</html>'):
            self._call_operation_object(sdn_const.DELETE, sdn_const.PORT)
            with mock.patch('requests.request',
                            return_value=self._response(requests.codes.ok,
                                                        text)):
                self.thread._sync_pending_rows(self.context, False)

            row = db.get_all_db_rows(self.context)[0]
            self.assertEqual(sdn_const.PENDING, row.state)
            self.assertEqual(1, row.retry_count)
            self._db_cleanup()

    def test_sync_progress_rows_job_error_is_retried(self):
        # A job the controller answers with an error for, such as one it no
        # longer knows, doesn't stop the other jobs being checked.
        for status_code in (requests.codes.not_found,
                            requests.codes.internal_server_error):
            self._call_operation_object(sdn_const.POST, sdn_const.NETWORK)
            self._call_operation_object(sdn_const.PUT, sdn_const.NETWORK)
            rows = sorted(db.get_all_db_rows(self.context),
                          key=lambda r: r.operation == sdn_const.PUT)
            for job_id, row in zip(('app/jobs/1', 'app/jobs/2'), rows):
                db.update_db_row_job_id(self.context, row, job_id)
                db.update_db_row_state(self.context, row,
                                       sdn_const.MONITORING)
            self._make_row_older(self.context, rows[0])

            def request(method, url, **kwargs):
                if url.endswith('app/jobs/1'):
                    return self._response(status_code)
                return self._response(requests.codes.ok,
                                      '{"Status": "Completed"}')

            with mock.patch('requests.request', side_effect=request):
                self.thread._sync_progress_rows(self.context)

            rows = {r.job_id: r for r in db.get_all_db_rows(self.context)}
            self.assertEqual(sdn_const.PENDING, rows['app/jobs/1'].state)
            self.assertEqual(1, rows['app/jobs/1'].retry_count)
            self.assertEqual(sdn_const.COMPLETED, rows['app/jobs/2'].state)
            self._db_cleanup()

    def test_sync_progress_rows_connection_error_keeps_monitoring(self):
        # The job may be fine, so it is checked again rather than the
        # request being sent again.
        self._call_operation_object(sdn_const.POST, sdn_const.NETWORK)
        row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)

        with mock.patch('requests.request',
                        side_effect=requests.exceptions.ConnectionError):
            self.thread._sync_progress_rows(self.context)

        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.MONITORING, row.state)
        self.assertEqual('app/jobs/1', row.job_id)
        self.assertEqual(0, row.retry_count)

    def test_sync_pending_rows_leaves_waiting_row_unchanged(self):
        # A row that has to wait isn't written to, so that rows waiting for
        # a long controller job don't add database writes on every sync.
        self._call_operation_object(sdn_const.DELETE, sdn_const.PORT)
        port_row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_state(self.context, port_row, sdn_const.MONITORING)
        self._call_operation_object(sdn_const.DELETE, sdn_const.NETWORK)
        network_row = db.get_all_db_rows_by_state(self.context,
                                                  sdn_const.PENDING)[0]
        last_retried = datetime.datetime(2020, 1, 1)
        self._set_row_time(self.context, network_row, last_retried)

        with self._limit_row_fetches(), \
                mock.patch('requests.request') as mock_request:
            self.thread._sync_pending_rows(self.context, False)

        mock_request.assert_not_called()
        row = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)[0]
        self.assertEqual(network_row.id, row.id)
        self.assertEqual(last_retried, row.last_retried)

    def test_get_retry_delays(self):
        self.assertEqual([10, 20, 40, 80, 160, journal.MAX_RETRY_DELAY],
                         journal.SdnJournalThread._get_retry_delays(10))
        self.assertEqual([journal.MAX_RETRY_DELAY],
                         journal.SdnJournalThread._get_retry_delays(
                             journal.MAX_RETRY_DELAY))
        # A sync_timeout longer than MAX_RETRY_DELAY is the only delay.
        self.assertEqual([600],
                         journal.SdnJournalThread._get_retry_delays(600))
        # Values below one second can't loop forever.
        for sync_timeout in (0, -10):
            delays = journal.SdnJournalThread._get_retry_delays(sync_timeout)
            self.assertEqual(1, delays[0])
            self.assertEqual(journal.MAX_RETRY_DELAY, delays[-1])

    @db_api.CONTEXT_WRITER
    def _set_row_retry(self, context, row, retry_count, last_retried):
        row.retry_count = retry_count
        row.last_retried = last_retried
        context.session.merge(row)

    def _test_sync_retried_row(self, seconds_ago):
        self._call_operation_object(sdn_const.PUT, sdn_const.NETWORK)
        row = db.get_all_db_rows(self.context)[0]
        last_retried = (datetime.datetime.utcnow().replace(microsecond=0) -
                        datetime.timedelta(seconds=seconds_ago))
        self._set_row_retry(self.context, row, 3, last_retried)

        response = self._response(requests.codes.accepted, '"app/jobs/1"')
        with self._limit_row_fetches(), \
                mock.patch('requests.request',
                           return_value=response) as mock_request:
            self.thread._sync_pending_rows(self.context, False)
        return mock_request, last_retried

    def test_sync_pending_rows_waits_before_retry(self):
        # The third retry waits four sync intervals.
        mock_request, last_retried = self._test_sync_retried_row(
            seconds_ago=cfg.CONF.sdn.sync_timeout)
        mock_request.assert_not_called()
        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.PENDING, row.state)
        self.assertEqual(last_retried, row.last_retried)

    def test_sync_pending_rows_retries_after_delay(self):
        mock_request, _ = self._test_sync_retried_row(
            seconds_ago=4 * cfg.CONF.sdn.sync_timeout + 1)
        self.assertEqual(1, mock_request.call_count)
        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.MONITORING, row.state)

    def test_sync_pending_rows_stops_when_all_rows_wait(self):
        # The network deletion waits for the port deletion, which the
        # controller is still running. The sync must return, so that the
        # job can be checked, rather than skip the same row forever.
        self._call_operation_object(sdn_const.DELETE, sdn_const.PORT)
        port_row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_state(self.context, port_row, sdn_const.MONITORING)
        self._call_operation_object(sdn_const.DELETE, sdn_const.NETWORK)

        with self._limit_row_fetches(), \
                mock.patch('requests.request') as mock_request:
            self.thread._sync_pending_rows(self.context, False)

        mock_request.assert_not_called()
        rows = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        self.assertEqual([sdn_const.NETWORK], [r.object_type for r in rows])

    def test_port(self):
        self._test_object_type(sdn_const.PORT)

    def test_port_update_pending_port_create(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.PORT, sdn_const.PUT, sdn_const.POST)

    def test_port_delete_pending_port_create(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.PORT, sdn_const.DELETE, sdn_const.POST)

    def test_port_delete_pending_port_update(self):
        self._test_object_operation_pending_object_operation(
            sdn_const.PORT, sdn_const.DELETE, sdn_const.PUT)

    def test_port_pending_network(self):
        self._test_object_type_pending_network(sdn_const.PORT)

    def test_port_processing_network(self):
        self._test_object_type_processing_network(sdn_const.PORT)

    def test_cleanup_processing_rows_time_not_expired(self):
        self._test_cleanup_processing_rows(datetime.datetime.utcnow(),
                                           sdn_const.PROCESSING)

    def test_cleanup_processing_rows_time_expired(self):
        old_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        self._test_cleanup_processing_rows(old_time, sdn_const.PENDING)

    def test_thread_call(self):
        """Verify that the sync thread method is called."""

        # The driver starts the sync thread when it is initialised, and
        # each operation sets the thread's sync event.
        self._call_operation_object(sdn_const.POST,
                                    sdn_const.NETWORK)

        # Verify that the thread call was made.
        self.assertTrue(self.mock_sync_thread.called)

    @db_api.CONTEXT_WRITER
    def _decrease_row_created_time(self, context, row):
        # Rows recorded before seqnum was added are ordered by created_at.
        row.created_at -= datetime.timedelta(hours=1)
        row.seqnum = None
        self.context.session.merge(row)

    def test_sync_multiple_updates(self):
        # add 2 updates
        for i in range(2):
            self._call_operation_object(sdn_const.PUT,
                                        sdn_const.NETWORK)

        # get the last update row
        last_row = db.get_all_db_rows(self.context)[-1]

        # change the last update created time
        self._decrease_row_created_time(self.context, last_row)

        # create 1 more operation to trigger the sync thread
        # verify that there are no calls to UFM controller, because the
        # first row was not valid (exit_after_run = true)
        self._test_thread_processing(sdn_const.PUT,
                                     sdn_const.NETWORK, expected_calls=0)

        # validate that all the rows are in 'pending' state
        # first row stays 'pending' because it was not valid
        rows = db.get_all_db_rows_by_state(self.context, sdn_const.PENDING)
        self.assertEqual(3, len(rows))

    def test__get_client_id_from_port(self):
        for opt_name in ('client-id', '61', 61):
            port = {'extra_dhcp_opts': [
                {'opt_name': 'tftp-server', 'opt_value': '10.0.0.1'},
                {'opt_name': opt_name, 'opt_value': CLIENT_ID}]}
            self.assertEqual(CLIENT_ID,
                             self.mech._get_client_id_from_port(port))
        self.assertIsNone(self.mech._get_client_id_from_port(
            {'extra_dhcp_opts': [{'opt_name': '60', 'opt_value': 'x'}]}))
        self.assertIsNone(self.mech._get_client_id_from_port({}))

    def test_create_baremetal_port_with_numeric_client_id(self):
        # Ironic sets the client ID option by number, which Neutron returns
        # as the string '61'. The port is sent when its binding is committed.
        context = self._get_mock_commit_operation_context()
        context.current.update({
            'binding:vnic_type': portbindings.VNIC_BAREMETAL,
            'binding:profile': {},
            'extra_dhcp_opts': [{'opt_name': '61', 'opt_value': CLIENT_ID,
                                 'ip_version': 4}]})
        context.original = dict(context.current)
        context.vif_type = portbindings.VIF_TYPE_OTHER

        self.mech.update_port_precommit(context)

        row = db.get_oldest_pending_db_row_with_lock(self.context)
        self.assertEqual(sdn_const.POST, row['operation'])
        self.assertEqual(sdn_const.PORT, row['object_type'])
        opts = jsonutils.loads(row['data'])['extra_dhcp_opts']
        self.assertEqual([{'opt_name': 'client-id', 'opt_value': CLIENT_ID,
                           'ip_version': 4}], opts)

    @staticmethod
    def _response(status_code, text=''):
        response = requests.Response()
        response.status_code = status_code
        response._content = text.encode()
        response.url = cfg.CONF.sdn.url
        return response

    @db_api.CONTEXT_WRITER
    def _make_row_older(self, context, row):
        row.last_retried -= datetime.timedelta(hours=1)
        context.session.merge(row)

    def _sync_port_and_network_rows(self, port_status_code):
        self._call_operation_object(sdn_const.POST, sdn_const.PORT)
        # Make the port row the oldest, so that it is synced first.
        self._make_row_older(self.context,
                             db.get_all_db_rows(self.context)[0])
        self._call_operation_object(sdn_const.PUT, sdn_const.NETWORK)

        def request(method, url, **kwargs):
            if url.endswith("/Port"):
                return self._response(port_status_code,
                                      'Missing attribute(s): client-id')
            return self._response(requests.codes.accepted, '"app/jobs/1"')

        with mock.patch('requests.request',
                        side_effect=request) as mock_request:
            self.thread._sync_pending_rows(self.context, False)
        rows = {row.object_type: row
                for row in db.get_all_db_rows(self.context)}
        return mock_request, rows[sdn_const.PORT], rows[sdn_const.NETWORK]

    def test_sync_rejected_row_fails_without_blocking(self):
        mock_request, port_row, network_row = (
            self._sync_port_and_network_rows(requests.codes.bad_request))

        self.assertEqual(2, mock_request.call_count)
        self.assertEqual(sdn_const.FAILED, port_row.state)
        self.assertEqual(0, port_row.retry_count)
        self.assertEqual(sdn_const.MONITORING, network_row.state)

    def test_sync_unavailable_row_is_retried(self):
        mock_request, port_row, network_row = (
            self._sync_port_and_network_rows(
                requests.codes.service_unavailable))

        self.assertEqual(1, mock_request.call_count)
        self.assertEqual(sdn_const.PENDING, port_row.state)
        self.assertEqual(1, port_row.retry_count)
        self.assertEqual(sdn_const.PENDING, network_row.state)

    def test_sync_rejected_delete_is_retried(self):
        # UFM rejects deleting a network while hosts are associated with it,
        # which can change, so the deletion must be retried.
        self._call_operation_object(sdn_const.DELETE, sdn_const.NETWORK)
        response = self._response(
            requests.codes.bad_request,
            'Network ID cannot be removed because it is associated to one '
            'or more hosts')
        with self._limit_row_fetches(), \
                mock.patch('requests.request', return_value=response):
            self.thread._sync_pending_rows(self.context, False)

        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.PENDING, row.state)
        self.assertEqual(1, row.retry_count)

    @db_api.CONTEXT_WRITER
    def _set_row_created_at(self, context, row, created_at):
        row.created_at = created_at
        context.session.merge(row)

    def _sync_port_rows_from_same_second(self, operations):
        """Record operations on a port in order, in one second, and sync

        The first row is made the first to be synced, if nothing stops it.
        """
        context = self._get_mock_port_operation_context()
        for operation in operations:
            db.create_pending_row(self.context, sdn_const.PORT,
                                  context.current['id'], operation,
                                  context.current)
        rows = sorted(db.get_all_db_rows(self.context),
                      key=lambda row: row.seqnum)
        self._make_row_older(self.context, rows[-1])
        # Store the same second in both rows, as MySQL's DATETIME does.
        now = datetime.datetime(2026, 10, 30, 12, 0, 0)
        for row in rows:
            self._set_row_created_at(self.context, row, now)

        response = self._response(requests.codes.accepted, '"app/jobs/1"')
        with mock.patch('requests.request',
                        return_value=response) as mock_request:
            self.thread._sync_pending_rows(self.context, False)
        return [c[0][0] for c in mock_request.call_args_list]

    def test_port_delete_then_post_from_same_second(self):
        # Moving a port to another host records the DELETE of its old
        # binding and the POST of the new one, usually in the same second.
        self.assertEqual(['DELETE'], self._sync_port_rows_from_same_second(
            [sdn_const.DELETE, sdn_const.POST]))
        rows = {r.operation: r for r in db.get_all_db_rows(self.context)}
        self.assertEqual(sdn_const.PENDING, rows[sdn_const.POST].state)
        self.assertEqual(sdn_const.MONITORING, rows[sdn_const.DELETE].state)

    def test_port_post_then_delete_from_same_second(self):
        # For example, a port unbound right after its binding.
        self.assertEqual(['POST'], self._sync_port_rows_from_same_second(
            [sdn_const.POST, sdn_const.DELETE]))
        rows = {r.operation: r for r in db.get_all_db_rows(self.context)}
        self.assertEqual(sdn_const.MONITORING, rows[sdn_const.POST].state)
        self.assertEqual(sdn_const.PENDING, rows[sdn_const.DELETE].state)

    def test_failed_delete_job_stops_holding_post(self):
        # A failed job counts towards retry_count, so the DELETE ends FAILED
        # and stops holding the POST that waits for it.
        self.conf.set_override('retry_count', 0, sdn_const.GROUP_OPT)
        self.thread = journal.SdnJournalThread()
        context = self._get_mock_port_operation_context()
        db.create_pending_row(self.context, sdn_const.PORT,
                              context.current['id'], sdn_const.DELETE,
                              context.current)
        row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)

        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': 'Failed'}))
        with mock.patch('requests.request', return_value=response):
            self.thread._sync_progress_rows(self.context)

        row = db.get_all_db_rows(self.context)[0]
        self.assertEqual(sdn_const.FAILED, row.state)
        self.assertFalse(db.check_for_pending_or_processing_ops(
            self.context, row.object_uuid))

    def test_cleanup_deletes_old_failed_rows(self):
        self._call_operation_object(sdn_const.POST, sdn_const.NETWORK)
        row = db.get_all_db_rows(self.context)[0]
        db.update_db_row_state(self.context, row, sdn_const.FAILED)
        self._make_row_older(self.context, row)

        cleanup.JournalCleanup().delete_completed_rows(self.context)

        self.assertEqual([], db.get_all_db_rows(self.context))

    def test_network_filter_phynset(self):
        self.conf.set_override(
            'physical_networks', 'datacenter', sdn_const.GROUP_OPT)
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self._test_filtered_object_type(sdn_const.NETWORK)

    def test_port_filter_phynset(self):
        self.conf.set_override(
            'physical_networks', 'datacenter', sdn_const.GROUP_OPT)
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self._test_filtered_object_type(sdn_const.PORT)

    def _test_filtered_object_type(self, object_type):
        # Add and process create request.
        for operation in (sdn_const.POST, sdn_const.PUT, sdn_const.DELETE):
            self._call_operation_object(operation, object_type)
            rows = db.get_all_db_rows(self.context)
            self.assertEqual(0, len(rows))


class SdnPkeyProvisioningTestCase(SdnConfigBase):
    """Ports are held DOWN until the controller adds them to their pkey"""

    HOST = 'r-ufm177'

    _db_cleanup = SdnDriverTestCase._db_cleanup
    _get_segments_list = SdnDriverTestCase._get_segments_list
    _get_mock_port_operation_context = (
        SdnDriverTestCase._get_mock_port_operation_context)
    _get_mock_bind_operation_context = (
        SdnDriverTestCase._get_mock_bind_operation_context)
    _get_mock_commit_operation_context = (
        SdnDriverTestCase._get_mock_commit_operation_context)
    _response = staticmethod(SdnDriverTestCase._response)
    _set_row_created_at = SdnDriverTestCase._set_row_created_at

    def setUp(self):
        super(SdnPkeyProvisioningTestCase, self).setUp()
        self.context = nl_context.get_admin_context()
        mock.patch.object(journal.SdnJournalThread,
                          'start_sync_thread').start()
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self.thread = journal.SdnJournalThread()
        self.addCleanup(self._db_cleanup)

    def _rows(self):
        return db.get_all_db_rows(self.context)

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_blocks_vlan_port(self, add_block):
        context = self._get_mock_commit_operation_context()
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_called_once_with(
            context._plugin_context, context.current['id'], resources.PORT,
            sdn_const.PROVISIONING_ENTITY)

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_does_not_block_flat_port(self, add_block):
        context = self._get_mock_commit_operation_context(
            net_type=constants.TYPE_FLAT)
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_not_called()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_after_failed_binding_blocks_port(self, add_block):
        context = self._get_mock_commit_operation_context()
        context.original_vif_type = portbindings.VIF_TYPE_BINDING_FAILED
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_called_once()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_activation_on_other_host_blocks_port(self, add_block):
        # Nova live migrates by activating a binding that is already bound
        # on the destination host.
        context = self._get_mock_commit_operation_context()
        context.original_vif_type = mech_mlnx.VIF_TYPE_IB_HOSTDEV
        context.original_host = 'old-host'
        context.original['binding:host_id'] = 'old-host'
        self.mech.update_port_precommit(context)

        rows = {r.operation: r for r in self._rows()}
        self.assertEqual({sdn_const.DELETE, sdn_const.POST}, set(rows))
        self.assertEqual('old-host', jsonutils.loads(
            rows[sdn_const.DELETE].data)['binding:host_id'])
        self.assertEqual(self.HOST, jsonutils.loads(
            rows[sdn_const.POST].data)['binding:host_id'])
        add_block.assert_called_once()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    @mock.patch.object(provisioning_blocks, 'remove_provisioning_component')
    def test_cold_migration_moves_port(self, remove_block, add_block):
        # Nova cold migrates or evacuates by updating the port's host, and
        # ML2 then binds and commits the binding on the new host.
        context = self._get_mock_port_operation_context()
        context.original = dict(context.current)
        context.original['binding:host_id'] = 'old-host'
        context.original_host = 'old-host'
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.DELETE],
                         [r.operation for r in self._rows()])
        remove_block.assert_called_once()
        add_block.assert_not_called()

        self._db_cleanup()
        self.mech.update_port_precommit(
            self._get_mock_commit_operation_context())

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_called_once()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_does_not_block_ethernet_port(self, add_block):
        context = self._get_mock_commit_operation_context()
        context.vif_type = portbindings.VIF_TYPE_HW_VEB
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_not_called()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_without_sync_does_nothing(self, add_block):
        self.conf.set_override('sync_enabled', False, sdn_const.GROUP_OPT)
        self.mech.update_port_precommit(
            self._get_mock_commit_operation_context())

        self.assertEqual([], self._rows())
        add_block.assert_not_called()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_update_of_bound_port_does_not_block(self, add_block):
        context = self._get_mock_commit_operation_context()
        context.original_vif_type = 'ib_hostdev'
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.PUT], [r.operation for r in self._rows()])
        add_block.assert_not_called()

    def test_binding_commit_fails_if_block_fails(self):
        # The exception must reach ML2, so that the binding fails rather than
        # leaving the port free to become ACTIVE.
        with mock.patch.object(provisioning_blocks,
                               'add_provisioning_component',
                               side_effect=RuntimeError):
            self.assertRaises(RuntimeError, self.mech.update_port_precommit,
                              self._get_mock_commit_operation_context())

    def test_bind_port_leaves_pkey_port_to_binding_commit(self):
        self.mech.bind_port(self._get_mock_bind_operation_context())
        self.assertEqual([], self._rows())

    def test_bind_port_sends_other_ports(self):
        context = self._get_mock_bind_operation_context()
        context.current['binding:vnic_type'] = portbindings.VNIC_NORMAL
        self.mech.bind_port(context)
        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])

    def _baremetal(self, context, client_id=CLIENT_ID):
        port = context.current
        port['binding:vnic_type'] = portbindings.VNIC_BAREMETAL
        port['device_owner'] = 'baremetal:none'
        port['binding:host_id'] = 'node-uuid'
        port['binding:profile'] = {'local_link_information': [{}]}
        if client_id:
            port['extra_dhcp_opts'] = [{'opt_name': 'client-id',
                                        'opt_value': client_id}]
        return context

    def _bind_baremetal(self, net_type=constants.TYPE_VLAN, **kwargs):
        context = self._baremetal(self._get_mock_bind_operation_context(),
                                  **kwargs)
        segments = self._get_segments_list(net_type=net_type)
        context.network.network_segments = segments
        context.segments_to_bind = segments
        self.mech.bind_port(context)
        return context.set_binding.call_args[0][3]

    def test_bind_ib_baremetal_port_on_vlan_leaves_it_down(self):
        self.assertIsNone(self._bind_baremetal())

    def test_bind_ib_baremetal_port_on_flat_makes_it_active(self):
        self.assertEqual(constants.PORT_STATUS_ACTIVE,
                         self._bind_baremetal(net_type=constants.TYPE_FLAT))

    def test_bind_ethernet_baremetal_port_makes_it_active(self):
        self.assertEqual(constants.PORT_STATUS_ACTIVE,
                         self._bind_baremetal(client_id=None))

    def test_host_update_of_ib_baremetal_port_waits_for_binding(self):
        # Ironic sets the host, client-id and local link information in one
        # update, and ML2 then binds the port.
        context = self._baremetal(self._get_mock_port_operation_context())
        context.original = dict(context.current)
        context.original.update({'binding:host_id': None,
                                 'extra_dhcp_opts': [],
                                 'binding:profile': {}})
        context.host = 'node-uuid'
        context.original_host = None
        self.mech.update_port_precommit(context)
        self.assertEqual([], self._rows())

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_blocks_ib_baremetal_port(self, add_block):
        context = self._baremetal(self._get_mock_commit_operation_context())
        context.host = context.original_host = 'node-uuid'
        context.vif_type = portbindings.VIF_TYPE_OTHER
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_called_once_with(
            context._plugin_context, context.current['id'], resources.PORT,
            sdn_const.PROVISIONING_ENTITY)

    @mock.patch.object(provisioning_blocks, 'remove_provisioning_component')
    def test_unbind_ib_baremetal_port_removes_block(self, remove_block):
        context = self._baremetal(self._get_mock_port_operation_context())
        context.original = dict(context.current)
        context.current = dict(context.current, **{'binding:host_id': ''})
        context.original_host = 'node-uuid'
        context.host = ''
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.DELETE],
                         [r.operation for r in self._rows()])
        remove_block.assert_called_once()

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_blocks_non_compute_port(self, add_block):
        context = self._get_mock_commit_operation_context()
        context.current['device_owner'] = 'compute-ish:service'
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.POST], [r.operation for r in self._rows()])
        add_block.assert_called_once()

    def _client_id_context(self, vnic_type, client_id, orig_client_id=None,
                           admin=False):
        context = self._get_mock_port_operation_context()
        context.current['binding:vnic_type'] = vnic_type
        context.original = dict(context.current)
        for port, value in ((context.current, client_id),
                            (context.original, orig_client_id)):
            port['extra_dhcp_opts'] = (
                [{'opt_name': 'client-id', 'opt_value': value}]
                if value else [])
        if not admin:
            context._plugin_context = nl_context.Context('user', 'project')
        return context

    def test_project_cant_set_client_id(self):
        for vnic_type in (portbindings.VNIC_DIRECT,
                          portbindings.VNIC_BAREMETAL):
            context = self._client_id_context(vnic_type, CLIENT_ID)
            self.assertRaises(n_exc.InvalidInput,
                              self.mech.create_port_precommit, context)
            self.assertRaises(n_exc.InvalidInput,
                              self.mech.update_port_precommit, context)
            context = self._client_id_context(vnic_type, CLIENT_ID,
                                              orig_client_id='20:00')
            self.assertRaises(n_exc.InvalidInput,
                              self.mech.update_port_precommit, context)

    def test_project_can_update_port_keeping_client_id(self):
        context = self._client_id_context(portbindings.VNIC_DIRECT,
                                          CLIENT_ID, orig_client_id=CLIENT_ID)
        self.mech.update_port_precommit(context)

    def test_project_cant_set_client_id_of_normal_port(self):
        # The port could become a direct or bare metal port later.
        context = self._client_id_context(portbindings.VNIC_NORMAL,
                                          CLIENT_ID)
        self.assertRaises(n_exc.InvalidInput,
                          self.mech.create_port_precommit, context)

    def test_project_cant_add_client_id_for_ipv6(self):
        context = self._client_id_context(portbindings.VNIC_DIRECT,
                                          CLIENT_ID, orig_client_id=CLIENT_ID)
        context.current['extra_dhcp_opts'] = (
            context.original['extra_dhcp_opts'] +
            [{'opt_name': 'client-id', 'opt_value': '20:00',
              'ip_version': 6}])
        self.assertRaises(n_exc.InvalidInput,
                          self.mech.update_port_precommit, context)

    def test_service_can_set_client_id(self):
        context = self._client_id_context(portbindings.VNIC_BAREMETAL,
                                          CLIENT_ID)
        context._plugin_context = nl_context.Context(
            'user', 'project', roles=['service'])
        self.mech.create_port_precommit(context)
        self.mech.update_port_precommit(context)

    def test_binding_commit_with_two_client_ids_fails(self):
        context = self._baremetal(self._get_mock_commit_operation_context())
        context.current['extra_dhcp_opts'].append(
            {'opt_name': '61', 'opt_value': '20:00'})
        context.original = dict(context.current)
        context.host = context.original_host = 'node-uuid'
        context.vif_type = portbindings.VIF_TYPE_OTHER
        self.assertRaises(n_exc.InvalidInput,
                          self.mech.update_port_precommit, context)
        self.assertEqual([], self._rows())

    def test_binding_commit_with_other_client_id_fails(self):
        # The VF's GUID is derived from the MAC address, so the controller
        # must not be given another one.
        context = self._get_mock_commit_operation_context()
        context.current['extra_dhcp_opts'] = [
            {'opt_name': 'client-id', 'opt_value': CLIENT_ID}]
        context.original = dict(context.current)
        self.assertRaises(n_exc.InvalidInput,
                          self.mech.update_port_precommit, context)
        self.assertEqual([], self._rows())

    @mock.patch.object(provisioning_blocks, 'add_provisioning_component')
    def test_binding_commit_with_generated_client_id(self, add_block):
        for prefix in (mech_mlnx.HARDWARE_CLIENT_ID_PREFIX,
                       mech_mlnx.LEGACY_CLIENT_ID_PREFIX):
            context = self._get_mock_commit_operation_context()
            client_id = (
                mech_mlnx.MlnxMechanismDriver._gen_client_id_with_prefix(
                    context.current, prefix))
            context.current['extra_dhcp_opts'] = [
                {'opt_name': 'client-id', 'opt_value': client_id.upper()}]
            context.original = dict(context.current)
            self.mech.update_port_precommit(context)
            self.assertEqual([sdn_const.POST],
                             [r.operation for r in self._rows()])
            self._db_cleanup()

    def test_delete_of_non_compute_direct_port_is_sent(self):
        context = self._get_mock_port_operation_context()
        context.current['device_owner'] = 'other:owner'
        self.mech.delete_port_precommit(context)
        self.assertEqual([sdn_const.DELETE],
                         [r.operation for r in self._rows()])

    @mock.patch.object(provisioning_blocks, 'remove_provisioning_component')
    def test_unbind_of_non_compute_direct_port_is_sent(self, remove_block):
        context = self._get_mock_port_operation_context()
        context.current['device_owner'] = 'other:owner'
        context.original = dict(context.current)
        context.current = dict(context.current, **{'binding:host_id': None})
        context.host = None
        self.mech.update_port_precommit(context)
        self.assertEqual([sdn_const.DELETE],
                         [r.operation for r in self._rows()])

    def test_admin_can_set_client_id(self):
        context = self._client_id_context(portbindings.VNIC_BAREMETAL,
                                          CLIENT_ID, admin=True)
        self.mech.create_port_precommit(context)
        self.mech.update_port_precommit(context)

    def test_create_ib_baremetal_port_is_not_sent(self):
        # It is sent when its binding is committed.
        context = self._client_id_context(portbindings.VNIC_BAREMETAL,
                                          CLIENT_ID, admin=True)
        self.mech.create_port_precommit(context)
        self.assertEqual([], self._rows())

    @mock.patch.object(provisioning_blocks, 'remove_provisioning_component')
    def test_unbind_removes_block(self, remove_block):
        context = self._get_mock_port_operation_context()
        context.original = dict(context.current)
        context.current['binding:host_id'] = None
        self.mech.update_port_precommit(context)

        self.assertEqual([sdn_const.DELETE],
                         [r.operation for r in self._rows()])
        remove_block.assert_called_once_with(
            context._plugin_context, context.current['id'], resources.PORT,
            sdn_const.PROVISIONING_ENTITY)

    def _make_blocked_port(self, row_host=HOST):
        """Create a port bound to HOST, with a block and its POST row"""
        with self.port(is_admin=True, device_owner=DEVICE_OWNER_COMPUTE,
                       arg_list=(portbindings.HOST_ID,),
                       **{portbindings.HOST_ID: self.HOST}) as port:
            port_id = port['port']['id']
            network_id = port['port']['network_id']
        self._db_cleanup()
        provisioning_blocks.add_provisioning_component(
            self.context, port_id, resources.PORT,
            sdn_const.PROVISIONING_ENTITY)
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST,
                              {'id': port_id, 'network_id': network_id,
                               'binding:host_id': row_host})
        return port_id, self._rows()[0]

    @db_api.CONTEXT_WRITER
    def _set_created_at_in_db(self, context, row, seconds):
        row.created_at += datetime.timedelta(seconds=seconds)
        context.session.merge(row)

    def _set_created_at(self, row, seconds):
        self._set_created_at_in_db(self.context, row, seconds)

    def _is_blocked(self, port_id):
        return db.port_has_provisioning_block(
            self.context, port_id, sdn_const.PROVISIONING_ENTITY)

    def _complete_job(self, row, status='Completed'):
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': status}))
        with mock.patch('requests.request', return_value=response), \
                mock.patch.object(provisioning_blocks,
                                  'provisioning_complete') as complete, \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.thread._sync_progress_rows(self.context)
        return complete, set_status

    def _sync_pending(self, response=None, side_effect=None):
        with mock.patch('requests.request', return_value=response,
                        side_effect=side_effect), \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.thread._sync_pending_rows(self.context, False)
        return set_status

    def _assert_port_failed(self, port_id, set_status):
        self.assertEqual(sdn_const.FAILED, self._rows()[0].state)
        set_status.assert_called_once_with(
            mock.ANY, port_id, constants.PORT_STATUS_ERROR)
        # The block stays, so that the agent can't make the port ACTIVE.
        self.assertTrue(self._is_blocked(port_id))

    def test_completed_job_releases_port_unmocked(self):
        # Runs provisioning_complete for real, which must not be called in a
        # transaction.
        port_id, row = self._make_blocked_port()
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        response = self._response(requests.codes.ok,
                                  jsonutils.dumps({'Status': 'Completed'}))
        with mock.patch('requests.request', return_value=response):
            self.thread._sync_progress_rows(self.context)

        self.assertFalse(self._is_blocked(port_id))

    def test_completed_job_with_older_post_releases_port(self):
        port_id, old_row = self._make_blocked_port()
        self._set_created_at(old_row, -60)
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST, jsonutils.loads(old_row.data))
        row = [r for r in self._rows() if r.id != old_row.id][0]
        complete, _set_status = self._complete_job(row)

        self.assertFalse(self._is_blocked(port_id))
        complete.assert_called_once()

    def test_completion_error_keeps_port_blocked(self):
        port_id, row = self._make_blocked_port()
        with mock.patch.object(provisioning_blocks,
                               'remove_provisioning_component',
                               side_effect=RuntimeError):
            complete, _set_status = self._complete_job(row)

        # The row is checked again, so that releasing the port is retried.
        self.assertEqual(sdn_const.MONITORING, self._rows()[0].state)
        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

    def test_completed_job_releases_port(self):
        port_id, row = self._make_blocked_port()
        complete, set_status = self._complete_job(row)

        self.assertEqual(sdn_const.COMPLETED, self._rows()[0].state)
        self.assertFalse(self._is_blocked(port_id))
        complete.assert_called_once_with(
            mock.ANY, port_id, resources.PORT, sdn_const.PROVISIONING_ENTITY)
        set_status.assert_not_called()

    def test_completed_job_for_other_host_keeps_port_blocked(self):
        # A late job for a binding that has since moved to another host
        # must not release the port's new binding.
        port_id, row = self._make_blocked_port(row_host='old-host')
        complete, _set_status = self._complete_job(row)

        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

    def test_completed_job_locks_binding(self):
        port_id, row = self._make_blocked_port()
        with mock.patch.object(db, 'lock_port_binding',
                               wraps=db.lock_port_binding) as lock:
            self._complete_job(row)

        lock.assert_called_once_with(mock.ANY, port_id)
        self.assertFalse(self._is_blocked(port_id))

    def test_failed_job_locks_binding(self):
        port_id, row = self._make_blocked_port()
        with mock.patch.object(db, 'lock_port_binding',
                               wraps=db.lock_port_binding) as lock:
            _complete, set_status = self._complete_job(row, status='Failed')

        lock.assert_called_with(mock.ANY, port_id)
        self._assert_port_failed(port_id, set_status)

    @db_api.CONTEXT_WRITER
    def _move_binding(self, context, port_id, host):
        context.session.query(ml2_models.PortBinding).filter_by(
            port_id=port_id).update({'host': host})

    def test_completed_job_after_binding_moved_keeps_port_blocked(self):
        # The port was rebound on another host after its POST was sent.
        port_id, row = self._make_blocked_port()
        self._move_binding(self.context, port_id, 'new-host')
        complete, set_status = self._complete_job(row)

        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

    def test_failed_job_after_binding_moved_does_not_fail_port(self):
        port_id, row = self._make_blocked_port()
        self._move_binding(self.context, port_id, 'new-host')
        _complete, set_status = self._complete_job(row, status='Failed')

        set_status.assert_not_called()

    def test_completed_job_of_newest_post_from_same_second(self):
        # Two binding commits in one second, for example an unbind and a
        # rebind on the same host. The newer POST releases the port.
        port_id, old_row = self._make_blocked_port()
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST, jsonutils.loads(old_row.data))
        rows = sorted(self._rows(), key=lambda row: row.seqnum)
        now = datetime.datetime(2026, 10, 30, 12, 0, 0)
        for row in rows:
            self._set_row_created_at(self.context, row, now)
        complete, _set_status = self._complete_job(rows[-1])

        self.assertFalse(self._is_blocked(port_id))
        complete.assert_called_once()

    def test_completed_job_with_newer_post_keeps_port_blocked(self):
        port_id, row = self._make_blocked_port()
        db.create_pending_row(self.context, sdn_const.PORT, port_id,
                              sdn_const.POST, jsonutils.loads(row.data))
        complete, _set_status = self._complete_job(row)

        self.assertTrue(self._is_blocked(port_id))
        complete.assert_not_called()

    def test_failed_job_fails_port(self):
        port_id, row = self._make_blocked_port()
        _complete, set_status = self._complete_job(row, status='Failed')
        self._assert_port_failed(port_id, set_status)

    def test_rejected_post_for_other_host_does_not_fail_port(self):
        port_id, _row = self._make_blocked_port(row_host='old-host')
        set_status = self._sync_pending(
            self._response(requests.codes.bad_request, 'Bad request'))

        self.assertEqual(sdn_const.FAILED, self._rows()[0].state)
        set_status.assert_not_called()
        self.assertTrue(self._is_blocked(port_id))

    def test_failed_job_for_other_host_is_retried(self):
        _port_id, row = self._make_blocked_port(row_host='old-host')
        _complete, set_status = self._complete_job(row, status='Failed')

        self.assertEqual(sdn_const.PENDING, self._rows()[0].state)
        self.assertEqual(1, self._rows()[0].retry_count)
        set_status.assert_not_called()

    def test_unreadable_job_failing_after_retries_fails_port(self):
        self.conf.set_override('retry_count', 0, sdn_const.GROUP_OPT)
        self.thread = journal.SdnJournalThread()
        port_id, row = self._make_blocked_port()
        db.update_db_row_job_id(self.context, row, 'app/jobs/1')
        db.update_db_row_state(self.context, row, sdn_const.MONITORING)
        with mock.patch('requests.request',
                        return_value=self._response(requests.codes.ok,
                                                    'not json')), \
                mock.patch.object(plugin.Ml2Plugin,
                                  'update_port_status') as set_status:
            self.thread._sync_progress_rows(self.context)
        self._assert_port_failed(port_id, set_status)

    def test_rejected_post_fails_port(self):
        port_id, _row = self._make_blocked_port()
        set_status = self._sync_pending(
            self._response(requests.codes.bad_request, 'Bad request'))
        self._assert_port_failed(port_id, set_status)

    def test_not_implemented_post_fails_port(self):
        port_id, _row = self._make_blocked_port()
        set_status = self._sync_pending(
            self._response(requests.codes.not_implemented))
        self._assert_port_failed(port_id, set_status)

    def test_not_implemented_post_of_unblocked_port_completes(self):
        port_id, _row = self._make_blocked_port()
        provisioning_blocks.remove_provisioning_component(
            self.context, port_id, resources.PORT,
            sdn_const.PROVISIONING_ENTITY)
        set_status = self._sync_pending(
            self._response(requests.codes.not_implemented))

        self.assertEqual(sdn_const.COMPLETED, self._rows()[0].state)
        set_status.assert_not_called()

    def test_post_without_job_id_fails_port(self):
        port_id, _row = self._make_blocked_port()
        set_status = self._sync_pending(
            self._response(requests.codes.ok, 'null'))
        self._assert_port_failed(port_id, set_status)

    def test_post_failing_after_retries_fails_port(self):
        self.conf.set_override('retry_count', 0, sdn_const.GROUP_OPT)
        self.thread = journal.SdnJournalThread()
        port_id, _row = self._make_blocked_port()
        set_status = self._sync_pending(
            self._response(requests.codes.service_unavailable))
        self._assert_port_failed(port_id, set_status)


class SdnSegmentCheckTestCase(SdnConfigBase):
    """Segments that would stop a network from being synced are refused"""

    def setUp(self):
        super(SdnSegmentCheckTestCase, self).setUp()
        self.context = nl_context.get_admin_context()
        mock.patch.object(journal.SdnJournalThread,
                          'start_sync_thread').start()
        mock.patch.object(journal.SdnJournalThread,
                          '_start_sync_timer').start()
        self.conf.set_override('physical_networks', ['physnet1'],
                               sdn_const.GROUP_OPT)
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self.network_id = self._make_network(
            self.fmt, 'net', True)['network']['id']
        # Start from no segments, rather than the test plugin's local one.
        network_obj.NetworkSegment.delete_objects(
            self.context, network_id=self.network_id)

    def _segment(self, physical_network, segmentation_id, is_dynamic=False,
                 network_type=constants.TYPE_VLAN):
        segment = network_obj.NetworkSegment(
            self.context, id=uuidutils.generate_uuid(),
            network_id=self.network_id,
            network_type=network_type,
            physical_network=physical_network,
            segmentation_id=segmentation_id, is_dynamic=is_dynamic,
            segment_index=segmentation_id)
        segment.create()
        return segment

    def _check(self, segment):
        payload = events.DBEventPayload(self.context, resource_id=segment.id,
                                        states=(segment,))
        self.mech._check_new_segment(resources.SEGMENT,
                                     events.PRECOMMIT_CREATE, None, payload)

    def test_unsynced_segment_on_synced_network_is_refused(self):
        self._segment('physnet1', 10)
        self.assertRaises(n_exc.InvalidInput, self._check,
                          self._segment('physnet2', 11))

    def test_synced_segment_on_synced_network_is_allowed(self):
        self._segment('physnet1', 10)
        self._check(self._segment('physnet1', 11))

    def test_unsynced_segment_on_unsynced_network_is_allowed(self):
        self._segment('physnet2', 10)
        self._check(self._segment('physnet3', 11))

    def test_synced_segment_on_unsynced_network_is_refused(self):
        # Whichever order the segments of a network are created in.
        self._segment('physnet2', 10)
        self.assertRaises(n_exc.InvalidInput, self._check,
                          self._segment('physnet1', 11))

    def test_first_segment_is_allowed(self):
        for physical_network in ('physnet1', 'physnet2'):
            self._check(self._segment(physical_network, 10))
            network_obj.NetworkSegment.delete_objects(
                self.context, network_id=self.network_id)

    def test_segment_without_physnet_on_synced_network_is_refused(self):
        self._segment('physnet1', 10)
        segment = self._segment(None, 11, network_type='vxlan')
        e = self.assertRaises(n_exc.InvalidInput, self._check, segment)
        self.assertIn('such as vxlan', str(e))

    def test_any_segment_is_allowed_when_all_physnets_are_synced(self):
        self.conf.set_override('physical_networks', ['*'],
                               sdn_const.GROUP_OPT)
        self.mech = sdn_mech_driver.SDNMechanismDriver()
        self.mech.initialize()
        self._segment('physnet1', 10)
        self._check(self._segment('physnet2', 11))

    def test_dynamic_segment_is_allowed(self):
        self._segment('physnet1', 10)
        self._check(self._segment('physnet2', 11, is_dynamic=True))

    def test_any_segment_is_allowed_without_sync(self):
        self.conf.set_override('sync_enabled', False, sdn_const.GROUP_OPT)
        self._segment('physnet1', 10)
        self._check(self._segment('physnet2', 11))

    def test_check_is_subscribed(self):
        self._segment('physnet1', 10)
        segment = self._segment('physnet2', 11)
        self.assertRaises(
            exceptions.CallbackFailure, registry.publish,
            resources.SEGMENT, events.PRECOMMIT_CREATE, self,
            payload=events.DBEventPayload(self.context,
                                          resource_id=segment.id,
                                          states=(segment,)))
