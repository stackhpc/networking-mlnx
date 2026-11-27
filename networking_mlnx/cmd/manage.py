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

"""Manage networking-mlnx's state in Neutron's database

The journal commands show and recover the rows of the mlnx_sdn_assist
journal. The journal records each change that mlnx_sdn_assist sends to
the SDN controller. A failed deletion of a port is kept until an operator
deals with it, because the port's GUID may still be in its partition key,
and every port with the same GUID is set to ERROR meanwhile.
"""

import datetime
import shutil
import sys

from neutron.common import config as common_config
from neutron_lib import context as nl_context
from neutron_lib import exceptions as n_exc
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_db import options as db_options
from oslo_serialization import jsonutils
import prettytable
import yaml

from networking_mlnx.db import db
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const

STATES = (sdn_const.PENDING, sdn_const.PROCESSING, sdn_const.MONITORING,
          sdn_const.COMPLETED, sdn_const.FAILED)
# The output follows the openstack client's. "list" shows a row's columns
# under these headings, which are also its keys in JSON and YAML. "show"
# shows its fields by their names, as "openstack port show" does.
COLUMNS = (('id', 'ID'), ('state', 'State'), ('operation', 'Operation'),
           ('object_type', 'Type'), ('object_uuid', 'Object'),
           ('retry_count', 'Retries'), ('last_retried', 'Last Tried'),
           ('job_id', 'Job'))
FORMATS = ('table', 'json', 'yaml', 'value')


def _is_port_delete(row):
    return (row.object_type == sdn_const.PORT and
            row.operation == sdn_const.DELETE)


def _sorted(value):
    """Sort the keys of nested dicts, so every format has one order"""
    if isinstance(value, dict):
        return {key: _sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted(item) for item in value]
    return value


def _value(value):
    """Return a value as JSON and YAML can show it"""
    if isinstance(value, datetime.datetime):
        # The journal's times are the database server's, in its time zone,
        # so no time zone is given.
        return value.strftime('%Y-%m-%dT%H:%M:%S')
    return _sorted(value)


def _text(value):
    """Return a value as a table cell or line of value output"""
    if isinstance(value, (list, dict)):
        return jsonutils.dumps(value, indent=2)
    return str(value)


def _select(args, names):
    """Return the indexes of the columns that -c/--column selects"""
    if not args.columns:
        return list(range(len(names)))
    unknown = [name for name in args.columns if name not in names]
    if unknown:
        raise ValueError('No such column: %s; choose from %s' % (
            ', '.join(unknown), ', '.join(names)))
    return [names.index(name) for name in args.columns]


def _fit(table, out, column):
    """Wrap a column so that the table fits the terminal, as cliff does"""
    if not out.isatty():
        return
    excess = (len(table.get_string().splitlines()[0]) -
              shutil.get_terminal_size().columns)
    if excess > 0:
        # The column's width, without its borders and padding.
        column_width = len(table.get_string(
            fields=[column]).splitlines()[0]) - 4
        table.max_width[column] = max(20, column_width - excess)


def _write(out, args, names, lines, single):
    """Write a list's lines, or a single item's name/value lines"""
    indexes = _select(args, names)
    names = [names[i] for i in indexes]
    lines = [[line[i] for i in indexes] for line in lines]
    if args.format == 'value':
        if single:
            for value in lines[0]:
                out.write(_text(value) + '\n')
        else:
            for line in lines:
                out.write(' '.join(_text(value) for value in line) + '\n')
    elif args.format == 'table':
        if single:
            table = prettytable.PrettyTable(['Field', 'Value'], align='l')
            for name, value in zip(names, lines[0]):
                table.add_row([name, _text(value)])
            _fit(table, out, 'Value')
        else:
            table = prettytable.PrettyTable(names, align='l')
            for line in lines:
                table.add_row([_text(value) for value in line])
            if 'Retries' in names:
                # pylint: disable=unsupported-assignment-operation
                table.align['Retries'] = 'r'
        out.write(table.get_string() + '\n')
    else:
        data = [dict(zip(names, line)) for line in lines]
        if single:
            data = data[0]
        if args.format == 'json':
            out.write(jsonutils.dumps(data, indent=2) + '\n')
        else:
            out.write(yaml.safe_dump(data, default_flow_style=False,
                                     sort_keys=False))


def list_rows(context, args, out, err):
    rows = db.get_rows(context, state=args.state, object_uuid=args.object)
    try:
        _write(out, args, [heading for _column, heading in COLUMNS],
               [[_value(getattr(row, column)) for column, _heading in COLUMNS]
                for row in rows], single=False)
    except ValueError as e:
        err.write('%s\n' % e)
        return 1
    return 0


def show_row(context, args, out, err):
    row = db.get_row(context, args.row)
    if row is None:
        err.write('Journal row %s does not exist\n' % args.row)
        return 1
    fields = {column: _value(getattr(row, column))
              for column, _heading in COLUMNS}
    fields['created_at'] = _value(row.created_at)
    data = jsonutils.loads(row.data or 'null')
    port = data if row.object_type == sdn_const.PORT and data else {}
    fields['guid'] = db.get_port_guid_identity(port) if port else None
    fields['network_id'] = (port.get('network_id') if port else
                            row.object_uuid
                            if row.object_type == sdn_const.NETWORK
                            else None)
    # For a failed port deletion, the ports with the same GUID, which can't
    # be added to a partition key until it is dealt with. None for other
    # rows, so that [] means there are none.
    fields['same_guid_ports'] = None
    if _is_port_delete(row) and row.state == sdn_const.FAILED:
        ports = db.get_ports_with_same_guid(context, row)
        fields['same_guid_ports'] = ports
        if args.format in ('table', 'value'):
            fields['same_guid_ports'] = '\n'.join(
                '%s (%s, %s)' % (port['id'], port['status'],
                                 'bound on %s' % port['host']
                                 if port['host'] else 'unbound')
                for port in ports)
    newer = db.get_newer_row(context, row)
    fields['superseded_by'] = newer.id if newer else None
    fields['data'] = _value(data)
    try:
        _write(out, args, list(fields), [list(fields.values())],
               single=True)
    except ValueError as e:
        err.write('%s\n' % e)
        return 1
    return 0


def retry_row(context, args, out, err):
    try:
        row = db.retry_failed_row(context, args.row)
    except n_exc.NeutronException as e:
        err.write('%s\n' % e)
        return 1
    out.write('Journal row %s, %s %s %s, will be sent again at the next '
              'sync\n' % (row.id, row.operation, row.object_type,
                          row.object_uuid))
    return 0


def _describe(row):
    """Describe a row in a line, with a port's GUID and network"""
    text = '%s: %s %s %s' % (row.id, row.operation, row.object_type,
                             row.object_uuid)
    if row.object_type == sdn_const.PORT:
        port = jsonutils.loads(row.data or 'null') or {}
        text += ' (GUID %s, network %s)' % (db.get_port_guid_identity(port),
                                            port.get('network_id'))
    return text


def _confirm(rows, err):
    err.write('Before deleting a failed row, check that the SDN controller '
              'has done what it records, or that it no longer needs doing. '
              'For a port deletion, check that the port\'s GUID is in no '
              'partition key on that network that it shouldn\'t be in; if '
              'the controller hasn\'t deleted the port, run "retry" '
              'instead.\n')
    for row in rows:
        err.write('  %s\n' % _describe(row))
    if not sys.stdin.isatty():
        err.write('Not deleting without --yes, since stdin is not a '
                  'terminal\n')
        return False
    err.write('Delete %s? [y/N] ' % ('it' if len(rows) == 1 else
                                     'these %d rows' % len(rows)))
    err.flush()
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        answer = ''
    if not answer.endswith('\n'):
        # Ctrl-C or Ctrl-D: end the prompt's line.
        err.write('\n')
    if answer.strip().lower() in ('y', 'yes'):
        return True
    err.write('Not deleting\n')
    return False


def delete_rows(context, args, out, err):
    """Delete failed rows, carrying on past those that can't be deleted"""
    ids = list(dict.fromkeys(args.rows))
    failed = []
    errors = 0
    for row_id in ids:
        row = db.get_row(context, row_id)
        if row is None:
            err.write('Journal row %s does not exist\n' % row_id)
            errors += 1
        elif row.state != sdn_const.FAILED:
            err.write('Journal row %s is %s, not failed\n' % (row_id,
                                                             row.state))
            errors += 1
        else:
            failed.append(row)
    if failed and not args.yes and not _confirm(failed, err):
        return 1
    for row in failed:
        try:
            db.delete_failed_row(context, row.id)
        except (n_exc.NeutronException, db_exc.DBError) as e:
            # For example, a row sent again since it was listed above, or
            # one locked by the journal's sync meanwhile.
            err.write("Can't delete journal row %s: %s\n" % (row.id, e))
            errors += 1
        else:
            out.write('Deleted journal row %s\n' % row.id)
    if errors:
        if len(ids) > 1:
            err.write('Failed to delete %d of %d journal rows\n' % (
                errors, len(ids)))
        return 1
    return 0


def _add_format_argument(parser):
    parser.add_argument('-f', '--format', choices=FORMATS, default='table',
                        help='The output format, table by default')
    parser.add_argument('-c', '--column', action='append', default=[],
                        dest='columns',
                        help='A column to include; repeat it for more. By '
                             'default every column is included')


def add_journal_parsers(subparsers):
    parser = subparsers.add_parser('list', help='List journal rows')
    parser.add_argument('--state', choices=STATES,
                        help='Only list rows in this state')
    parser.add_argument('--object', help='Only list rows of this network or '
                                         'port ID')
    _add_format_argument(parser)
    parser.set_defaults(func=list_rows)

    parser = subparsers.add_parser(
        'show', help='Show a journal row, and for a failed port deletion, '
                     'the ports with the same GUID, which it keeps out of '
                     'partition keys')
    parser.add_argument('row', help='Row ID')
    _add_format_argument(parser)
    parser.set_defaults(func=show_row)

    parser = subparsers.add_parser(
        'retry', help='Send a failed port deletion to the SDN controller '
                      'again, so that it confirms that it is done. Refused '
                      'if that could undo something newer')
    parser.add_argument('row', help='Row ID')
    parser.set_defaults(func=retry_row)

    parser = subparsers.add_parser(
        'delete', help='Delete failed rows, once you have checked that the '
                       'SDN controller has done what they record')
    parser.add_argument('rows', metavar='row', nargs='+',
                        help='One or more row IDs')
    parser.add_argument('--yes', action='store_true',
                        help="Don't ask for confirmation")
    parser.set_defaults(func=delete_rows)


def add_command_parsers(subparsers):
    summary = "Show and recover the rows of the mlnx_sdn_assist journal"
    parser = subparsers.add_parser('journal', help=summary,
                                   description=summary)
    journal = parser.add_subparsers(title='Journal commands',
                                    dest='journal_command', metavar='COMMAND')
    journal.required = True
    add_journal_parsers(journal)


def setup_conf(conf, argv):
    common_config.register_common_config_options()
    db_group, db_opts = db_options.list_opts()[0]
    conf.register_opts(db_opts, db_group)
    conf.register_cli_opt(cfg.SubCommandOpt(
        'command', title='Commands', handler=add_command_parsers))
    conf(argv, project='neutron')


def main(argv=None):
    conf = cfg.CONF
    try:
        setup_conf(conf, sys.argv[1:] if argv is None else argv)
    except cfg.Error as e:
        sys.stderr.write('%s\n' % e)
        return 1
    context = nl_context.get_admin_context()
    try:
        return conf.command.func(context, conf.command, sys.stdout,
                                 sys.stderr)
    except db_exc.DBError as e:
        sys.stderr.write("Can't use Neutron's database: %s\n" % e)
        return 1
