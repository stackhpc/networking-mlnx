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

from neutron.plugins.ml2 import plugin
from neutron.tests.unit.plugins.ml2 import test_plugin
from neutron.tests.unit import testlib_api
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants
from neutron_lib import context as nl_context
from neutron_lib.db import api as db_api
from oslo_config import cfg
from oslo_config import fixture as fixture_config
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests

from networking_mlnx.db import db
from networking_mlnx.db import schema
from networking_mlnx.journal import cleanup
from networking_mlnx.journal import journal
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
                            network=network_context)
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
            context = self._get_mock_bind_operation_context()
            method = getattr(self.mech, 'bind_port')
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

        # Create any object that would spin up the sync thread via the
        # decorator call_thread_on_end() used by all the event handlers.
        self._call_operation_object(sdn_const.POST,
                                    sdn_const.NETWORK)

        # Verify that the thread call was made.
        self.assertTrue(self.mock_sync_thread.called)

    @db_api.CONTEXT_WRITER
    def _decrease_row_created_time(self, context, row):
        row.created_at -= datetime.timedelta(hours=1)
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
        # as the string '61'.
        context = self._get_mock_port_operation_context()
        context.current.update({
            'binding:vnic_type': portbindings.VNIC_BAREMETAL,
            'binding:profile': {},
            'extra_dhcp_opts': [{'opt_name': '61', 'opt_value': CLIENT_ID,
                                 'ip_version': 4}]})

        self.mech.create_port_precommit(context)

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
