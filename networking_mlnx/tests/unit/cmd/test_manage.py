# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import io
import os
import unittest.mock as mock

import netaddr

from neutron.objects import network as network_obj
from neutron.objects.port.extensions import extra_dhcp_opt as edo_obj
from neutron.objects import ports as ports_obj
from neutron.tests.unit import testlib_api
from neutron_lib import constants as n_const
from neutron_lib import context as nl_context
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import requests
import yaml

from networking_mlnx.cmd import manage
from networking_mlnx.db import db
from networking_mlnx.journal import journal
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const
from networking_mlnx.plugins.ml2.drivers.sdn import exceptions as sdn_exc

MAC = 'fa:16:3e:00:00:01'
CLIENT_ID = '20:00:00:00:00:00:00:00:00:00:00:00:00:00:02:c9:03:00:00:00:01'


class SdnJournalCommandTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.context = nl_context.get_admin_context()
        self.network_id = uuidutils.generate_uuid()
        network_obj.Network(self.context, id=self.network_id,
                            name='ib').create()

    def _create_port(self, mac=MAC, vnic_type='direct', client_id=None,
                     status=n_const.PORT_STATUS_ERROR, bound=True,
                     port_id=None, opt_name='client-id'):
        port = ports_obj.Port(
            self.context, id=port_id or uuidutils.generate_uuid(),
            network_id=self.network_id,
            # As the API stores it.
            mac_address=netaddr.EUI(mac, dialect=netaddr.mac_unix_expanded),
            device_id='',
            device_owner='', admin_state_up=True, status=status)
        port.create()
        ports_obj.PortBinding(
            self.context, port_id=port.id, host='host' if bound else '',
            vif_type='ib_hostdev' if bound else 'unbound',
            vnic_type=vnic_type, status=n_const.ACTIVE).create()
        if client_id:
            edo_obj.ExtraDhcpOpt(self.context, port_id=port.id,
                                 opt_name=opt_name, opt_value=client_id,
                                 ip_version=4).create()
        return port.id

    def _record(self, operation, object_uuid=None, state=None,
                object_type=sdn_const.PORT, data=None):
        object_uuid = object_uuid or uuidutils.generate_uuid()
        if data is None:
            data = {'id': object_uuid, 'network_id': self.network_id,
                    'mac_address': MAC, 'binding:vnic_type': 'direct'}
        db.create_pending_row(self.context, object_type, object_uuid,
                              operation, data)
        row = db.get_rows(self.context, object_uuid=object_uuid)[-1]
        if state:
            db.update_db_row_state(self.context, row, state)
        return row

    def _run(self, func, **kwargs):
        """Run a command; return its exit code, stdout and stderr"""
        out = io.StringIO()
        err = io.StringIO()
        kwargs.setdefault('format', 'table')
        kwargs.setdefault('columns', [])
        args = argparse.Namespace(**kwargs)
        code = func(self.context, args, out, err)
        return code, out.getvalue() + err.getvalue()

    def _run_data(self, func, fmt, **kwargs):
        """Run a command with JSON or YAML output; return what it shows"""
        out = io.StringIO()
        err = io.StringIO()
        kwargs.setdefault('columns', [])
        args = argparse.Namespace(format=fmt, **kwargs)
        self.assertEqual(0, func(self.context, args, out, err))
        self.assertEqual('', err.getvalue())
        if fmt == 'value':
            return out.getvalue()
        load = jsonutils.loads if fmt == 'json' else yaml.safe_load
        return load(out.getvalue())

    def test_list(self):
        failed = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        pending = self._record(sdn_const.POST)
        code, out = self._run(manage.list_rows, state=None, object=None)
        self.assertEqual(0, code)
        # A table, as the openstack client shows.
        self.assertTrue(out.startswith('+--'))
        self.assertIn('| ID ', out.splitlines()[1])
        self.assertIn(failed.id, out)
        self.assertIn(pending.id, out)

        code, out = self._run(manage.list_rows, state=sdn_const.FAILED,
                              object=None)
        self.assertIn(failed.id, out)
        self.assertNotIn(pending.id, out)

        code, out = self._run(manage.list_rows, state=None,
                              object=pending.object_uuid)
        self.assertNotIn(failed.id, out)
        self.assertIn(pending.id, out)

    def test_show_failed_port_delete_lists_ports_with_same_guid(self):
        held = self._create_port()
        self._create_port(mac='fa:16:3e:00:00:02')
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        code, out = self._run(manage.show_row, row=row.id)
        self.assertEqual(0, code)
        self.assertIn('| guid ', out)
        self.assertIn(MAC, out)
        self.assertIn('%s (ERROR, bound on host)' % held, out)

        for fmt in ('json', 'yaml'):
            fields = self._run_data(manage.show_row, fmt, row=row.id)
            self.assertEqual(
                ['id', 'state', 'operation', 'object_type', 'object_uuid',
                 'retry_count', 'last_retried', 'job_id', 'created_at',
                 'guid', 'network_id', 'same_guid_ports', 'superseded_by',
                 'data'], list(fields))
            self.assertEqual(row.id, fields['id'])
            self.assertEqual(MAC, fields['guid'])
            self.assertEqual(self.network_id, fields['network_id'])
            self.assertEqual([{'id': held, 'status': 'ERROR',
                               'network_id': self.network_id,
                               'host': 'host'}],
                             fields['same_guid_ports'])
            self.assertEqual(MAC, fields['data']['mac_address'])
            self.assertEqual(sorted(fields['data']), list(fields['data']))
            self.assertIsNone(fields['superseded_by'])
            self.assertIsNone(fields['job_id'])
            self.assertRegex(fields['created_at'],
                             r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$')

        self.assertEqual(
            '%s\n%s (ERROR, bound on host)\n' % (row.id, held),
            self._run_data(manage.show_row, 'value', row=row.id,
                           columns=['id', 'same_guid_ports']))

    def test_show_network_row(self):
        row = self._record(sdn_const.POST, object_type=sdn_const.NETWORK,
                           data={'id': self.network_id})
        fields = self._run_data(manage.show_row, 'json', row=row.id)
        # Every key is shown, null when it doesn't apply.
        self.assertIsNone(fields['guid'])
        self.assertIsNone(fields['same_guid_ports'])
        self.assertEqual(row.object_uuid, fields['network_id'])

    def test_ports_with_same_guid_bare_metal(self):
        # A bare metal port is identified by its client ID, not its MAC
        # address, which a virtual function's port may share.
        bare_metal = self._create_port(mac='fa:16:3e:00:00:03',
                                       vnic_type='baremetal',
                                       client_id=CLIENT_ID.upper())
        self._create_port(mac=MAC, vnic_type='baremetal',
                          client_id='ff:00:00:00:00:00:00:00:00:00:00:00:00:'
                                    '00:02:c9:03:00:00:00:09')
        row = self._record(
            sdn_const.DELETE, state=sdn_const.FAILED,
            data={'id': 'old', 'mac_address': MAC,
                  'binding:vnic_type': 'baremetal',
                  'extra_dhcp_opts': [{'opt_name': 'client-id',
                                       'opt_value': CLIENT_ID}]})
        self.assertEqual([bare_metal],
                         [port['id'] for port in
                          db.get_ports_with_same_guid(self.context, row)])

    def test_ports_with_same_guid_by_option_number(self):
        # Ironic sets the client ID by its option number. The row's own
        # port isn't another port with its GUID.
        bare_metal = self._create_port(mac='fa:16:3e:00:00:03',
                                       vnic_type='baremetal',
                                       client_id=CLIENT_ID, opt_name='61')
        own = self._create_port(mac='fa:16:3e:00:00:04',
                                vnic_type='baremetal', client_id=CLIENT_ID)
        row = self._record(
            sdn_const.DELETE, object_uuid=own, state=sdn_const.FAILED,
            data={'id': own, 'binding:vnic_type': 'baremetal',
                  'extra_dhcp_opts': [{'opt_name': '61',
                                       'opt_value': CLIENT_ID}]})
        self.assertEqual([bare_metal],
                         [port['id'] for port in
                          db.get_ports_with_same_guid(self.context, row)])

    def test_list_json_and_yaml(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        for fmt in ('json', 'yaml'):
            rows = self._run_data(manage.list_rows, fmt, state=None,
                                  object=None)
            self.assertEqual(1, len(rows))
            self.assertEqual(
                ['ID', 'State', 'Operation', 'Type', 'Object', 'Retries',
                 'Last Tried', 'Job'], list(rows[0]))
            self.assertEqual(row.id, rows[0]['ID'])
            self.assertEqual(0, rows[0]['Retries'])
            self.assertIsNone(rows[0]['Job'])
        # As openstack -f value -c ID, for scripts.
        self.assertEqual(
            '%s failed\n' % row.id,
            self._run_data(manage.list_rows, 'value', state=None,
                           object=None, columns=['ID', 'State']))
        code, out = self._run(manage.list_rows, state=None,
                              object=None, columns=['Nope'])
        self.assertEqual(1, code)
        self.assertIn('No such column: Nope', out)
        self.assertEqual([], self._run_data(
            manage.list_rows, 'json', state=sdn_const.PENDING,
            object=None))

    def test_show_table_fits_terminal(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED,
                           data={'id': 'port', 'description': 'x' * 200})
        out = io.StringIO()
        out.isatty = lambda: True
        args = argparse.Namespace(row=row.id, format='table', columns=[])
        with mock.patch('shutil.get_terminal_size',
                        return_value=os.terminal_size((100, 50))):
            self.assertEqual(0, manage.show_row(self.context, args, out,
                                                io.StringIO()))
        self.assertLessEqual(max(map(len, out.getvalue().splitlines())), 100)

    def test_show_missing_row(self):
        code, out = self._run(manage.show_row, row='missing')
        self.assertEqual(1, code)
        self.assertIn('does not exist', out)

    def test_retry(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        row.retry_count = 5
        row.job_id = 'app/jobs/1'
        db.update_db_row_state(self.context, row, sdn_const.FAILED)
        code, out = self._run(manage.retry_row, row=row.id)
        self.assertEqual(0, code)
        self.assertIn('will be sent again', out)
        row = db.get_row(self.context, row.id)
        self.assertEqual(sdn_const.PENDING, row.state)
        self.assertEqual(0, row.retry_count)
        self.assertIsNone(row.job_id)

    def test_retry_completes_when_controller_has_no_port(self):
        # The journal worker sends the retried deletion, and the controller's
        # 404 shows that it is done.
        cfg.CONF.set_override('url', 'http://127.0.0.1/ufmRestV3', 'sdn')
        cfg.CONF.set_override('token', 'token', 'sdn')
        self.addCleanup(cfg.CONF.clear_override, 'url', 'sdn')
        self.addCleanup(cfg.CONF.clear_override, 'token', 'sdn')
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        self.assertEqual(0, self._run(manage.retry_row, row=row.id)[0])
        response = requests.Response()
        response.status_code = requests.codes.not_found
        response._content = b''
        worker = journal.SdnJournalWorker()
        with mock.patch('requests.request', return_value=response) as send:
            worker._sync_pending_rows(self.context, False)
        self.assertTrue(send.call_args.kwargs['url'].endswith(
            row.object_uuid))
        self.assertEqual(sdn_const.COMPLETED,
                         db.get_row(self.context, row.id).state)

    def test_retry_refuses_other_than_port_deletes(self):
        for object_type, operation in ((sdn_const.PORT, sdn_const.POST),
                                       (sdn_const.NETWORK,
                                        sdn_const.DELETE)):
            row = self._record(operation, object_type=object_type,
                               state=sdn_const.FAILED)
            code, out = self._run(manage.retry_row, row=row.id)
            self.assertEqual(1, code)
            self.assertIn('only failed port deletions', out)

    def test_retry_refuses_port_bound_again(self):
        # Its newer POST may have completed and been deleted already.
        port_id = self._create_port()
        row = self._record(sdn_const.DELETE, object_uuid=port_id,
                           state=sdn_const.FAILED)
        code, out = self._run(manage.retry_row, row=row.id)
        self.assertEqual(1, code)
        self.assertIn('is bound again, on host host', out)
        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)

    def test_retry_with_unbound_port(self):
        port_id = self._create_port(bound=False)
        row = self._record(sdn_const.DELETE, object_uuid=port_id,
                           state=sdn_const.FAILED)
        self.assertEqual(0, self._run(manage.retry_row, row=row.id)[0])

    def test_retry_refuses_port_with_same_guid_bound(self):
        other = self._create_port()
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        code, out = self._run(manage.retry_row, row=row.id)
        self.assertEqual(1, code)
        self.assertIn('port %s, with the same GUID, is bound' % other, out)

        # Not when the bound port is on another network, or unbound.
        object_uuid = uuidutils.generate_uuid()
        row = self._record(
            sdn_const.DELETE, object_uuid=object_uuid, state=sdn_const.FAILED,
            data={'id': object_uuid, 'network_id': 'other-network',
                  'mac_address': MAC, 'binding:vnic_type': 'direct'})
        self.assertEqual(0, self._run(manage.retry_row, row=row.id)[0])
        self._create_port(mac='fa:16:3e:00:00:05', bound=False)
        row = self._record(
            sdn_const.DELETE, state=sdn_const.FAILED,
            data={'id': 'x', 'network_id': self.network_id,
                  'mac_address': 'fa:16:3e:00:00:05',
                  'binding:vnic_type': 'direct'})
        self.assertEqual(0, self._run(manage.retry_row, row=row.id)[0])

    def test_retry_refuses_superseded_row(self):
        # Sending the old deletion again would remove the GUID that the
        # newer POST added.
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        newer = self._record(sdn_const.POST, object_uuid=row.object_uuid,
                             state=sdn_const.COMPLETED)
        code, out = self._run(manage.retry_row, row=row.id)
        self.assertEqual(1, code)
        self.assertIn(newer.id, out)
        self.assertEqual(sdn_const.FAILED,
                         db.get_row(self.context, row.id).state)

    def test_retry_and_delete_refuse_unfailed_row(self):
        row = self._record(sdn_const.DELETE)
        for func, kwargs in ((manage.retry_row, {'row': row.id}),
                             (manage.delete_rows, {'rows': [row.id]})):
            code, out = self._run(func, yes=True, **kwargs)
            self.assertEqual(1, code)
            self.assertNotIn('Failed to delete', out)
            self.assertIn('is pending, not failed', out)
        self.assertIsNotNone(db.get_row(self.context, row.id))

    def test_delete_with_yes(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        code, out = self._run(manage.delete_rows, rows=[row.id], yes=True)
        self.assertEqual(0, code)
        self.assertIsNone(db.get_row(self.context, row.id))

    def test_delete_asks(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        for answer, deleted in (('n\n', False), ('y\n', True)):
            with mock.patch('sys.stdin') as stdin:
                stdin.isatty.return_value = True
                stdin.readline.return_value = answer
                code, out = self._run(manage.delete_rows, rows=[row.id],
                                      yes=False)
            self.assertIn("check that the port's GUID", out)
            self.assertEqual(deleted,
                             db.get_row(self.context, row.id) is None)
            self.assertEqual(0 if deleted else 1, code)

    def test_delete_without_terminal_needs_yes(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        with mock.patch('sys.stdin') as stdin:
            stdin.isatty.return_value = False
            code, out = self._run(manage.delete_rows, rows=[row.id],
                                  yes=False)
        self.assertEqual(1, code)
        self.assertIn('--yes', out)
        self.assertIsNotNone(db.get_row(self.context, row.id))

    def test_delete_several(self):
        rows = [self._record(sdn_const.DELETE, state=sdn_const.FAILED)
                for _ in range(2)]
        ids = [row.id for row in rows]
        with mock.patch('sys.stdin') as stdin:
            stdin.isatty.return_value = True
            stdin.readline.return_value = 'y\n'
            code, out = self._run(manage.delete_rows, rows=ids + ids[:1],
                                  yes=False)
        self.assertEqual(0, code)
        # One question for all of them, once each.
        stdin.readline.assert_called_once()
        self.assertIn('Delete these 2 rows?', out)
        for row_id in ids:
            self.assertIn('  %s: DELETE Port' % row_id, out)
        self.assertIn('(GUID %s, network %s)' % (MAC, self.network_id), out)
        for row_id in ids:
            self.assertEqual(1, out.count('Deleted journal row %s' % row_id))
            self.assertIsNone(db.get_row(self.context, row_id))

    def test_delete_carries_on_past_rows_it_cannot_delete(self):
        failed = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        pending = self._record(sdn_const.DELETE)
        code, out = self._run(manage.delete_rows,
                              rows=['missing', pending.id, failed.id],
                              yes=True)
        self.assertEqual(1, code)
        self.assertIn('Journal row missing does not exist', out)
        self.assertIn('is pending, not failed', out)
        self.assertIn('Failed to delete 2 of 3 journal rows', out)
        self.assertIsNone(db.get_row(self.context, failed.id))
        self.assertIsNotNone(db.get_row(self.context, pending.id))

    def test_delete_several_declined(self):
        failed = [self._record(sdn_const.DELETE, state=sdn_const.FAILED)
                  for _ in range(2)]
        pending = self._record(sdn_const.DELETE)
        for tty, answer in ((True, 'n\n'), (True, ''), (False, None)):
            with mock.patch('sys.stdin') as stdin:
                stdin.isatty.return_value = tty
                stdin.readline.return_value = answer
                code, out = self._run(
                    manage.delete_rows,
                    rows=[pending.id] + [row.id for row in failed], yes=False)
            self.assertEqual(1, code)
            self.assertIn('is pending, not failed', out)
            # The question lists only the rows it would delete.
            self.assertNotIn('  %s:' % pending.id, out)
            self.assertIn('--yes' if not tty else 'Not deleting', out)
            self.assertNotIn('Deleted', out)
            for row in failed:
                self.assertIn('  %s:' % row.id, out)
                self.assertIsNotNone(db.get_row(self.context, row.id))

    def test_delete_interrupted_at_question(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        with mock.patch('sys.stdin') as stdin:
            stdin.isatty.return_value = True
            stdin.readline.side_effect = KeyboardInterrupt
            code, out = self._run(manage.delete_rows, rows=[row.id],
                                  yes=False)
        self.assertEqual(1, code)
        self.assertIn('Not deleting', out)
        self.assertIsNotNone(db.get_row(self.context, row.id))

    def test_delete_asks_nothing_when_nothing_is_failed(self):
        pending = self._record(sdn_const.DELETE)
        with mock.patch('sys.stdin') as stdin:
            code, out = self._run(manage.delete_rows,
                                  rows=['missing', pending.id], yes=False)
        stdin.isatty.assert_not_called()
        stdin.readline.assert_not_called()
        self.assertEqual(1, code)
        self.assertIn('Failed to delete 2 of 2 journal rows', out)

    def test_delete_carries_on_past_database_errors(self):
        rows = [self._record(sdn_const.DELETE, state=sdn_const.FAILED)
                for _ in range(2)]
        delete = db.delete_failed_row

        def delete_failed_row(context, row_id):
            if row_id == rows[0].id:
                raise db_exc.DBError('lock wait timeout')
            return delete(context, row_id)

        with mock.patch.object(db, 'delete_failed_row',
                               side_effect=delete_failed_row):
            code, out = self._run(
                manage.delete_rows,
                rows=[rows[0].id, rows[1].id, rows[0].id], yes=True)
        self.assertEqual(1, code)
        self.assertIn("Can't delete journal row %s: lock wait timeout" %
                      rows[0].id, out)
        # The same ID twice counts once.
        self.assertIn('Failed to delete 1 of 2 journal rows', out)
        self.assertIsNotNone(db.get_row(self.context, rows[0].id))
        self.assertIsNone(db.get_row(self.context, rows[1].id))

    def test_delete_row_failing_meanwhile_is_reported(self):
        row = self._record(sdn_const.DELETE, state=sdn_const.FAILED)
        with mock.patch.object(db, 'delete_failed_row',
                               side_effect=sdn_exc.JournalRowNotFailed(
                                   row_id=row.id, state='pending')):
            code, out = self._run(manage.delete_rows, rows=[row.id],
                                  yes=True)
        self.assertEqual(1, code)
        self.assertIn('is pending, not failed', out)
        self.assertNotIn('Deleted', out)

    def test_parse_arguments(self):
        conf = cfg.ConfigOpts()
        manage.setup_conf(conf, ['journal', 'retry', 'row-id'])
        self.assertEqual('journal', conf.command.name)
        self.assertEqual('row-id', conf.command.row)
        self.assertIs(manage.retry_row, conf.command.func)
        self.assertIn('connection', conf.database)
        conf = cfg.ConfigOpts()
        manage.setup_conf(conf, ['journal', 'delete', '--yes', 'a', 'b'])
        self.assertEqual(['a', 'b'], conf.command.rows)
        self.assertIs(manage.delete_rows, conf.command.func)

    def test_journal_needs_a_command(self):
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertRaises(SystemExit, manage.setup_conf,
                              cfg.ConfigOpts(), ['journal'])
        self.assertIn('COMMAND', err.getvalue())
