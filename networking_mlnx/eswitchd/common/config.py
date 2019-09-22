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

import shlex
import sys

from neutron.conf.agent import common as common_conf
from oslo_config import cfg
from oslo_log import log as logging
from oslo_privsep import priv_context

LOG = logging.getLogger(__name__)
DEFAULT_INTERFACE_MAPPINGS = []

mlx_daemon_opts = [
    cfg.StrOpt('socket_os_transport', default="tcp"),
    cfg.StrOpt('socket_os_port', default="60001"),
    cfg.StrOpt('socket_os_addr', default="0.0.0.0"),
    cfg.ListOpt('fabrics',
                default=DEFAULT_INTERFACE_MAPPINGS,
                help=("List of <physical_network>:<physical_interface>")),
    cfg.IntOpt('default_timeout',
               default=5000,
               help=('Default timeout waiting for messages')),
    cfg.IntOpt('max_polling_count',
               default=5,
               help=('Daemon will do sync after max_polling_count '
                     '* default_timeout')),
]


def register_mlnx_daemon_opts(conf=cfg.CONF):
    conf.register_opts(mlx_daemon_opts, "DAEMON")


def register_opts(conf=cfg.CONF):
    # NOTE(adrianc) Logging opts are registered during import of
    # common_conf module.
    common_conf.register_root_helper(conf)
    register_mlnx_daemon_opts(conf)


def setup_logging(conf=cfg.CONF):
    """Sets up the logging options for a log with supplied name."""
    logging.setup(conf, 'eswitchd')
    LOG.info("Logging enabled!")
    LOG.info("%(prog)s Started!",
             {'prog': sys.argv[0]})
    LOG.debug("command line: %s", " ".join(sys.argv))


def get_root_helper(conf=cfg.CONF):
    return conf.AGENT.root_helper


def setup_privsep(conf=cfg.CONF):
    priv_context.init(root_helper=shlex.split(get_root_helper(conf)))


def init(args, **kwargs):
    cfg.CONF(args=args, project='eswitchd',
             **kwargs)
    register_opts(cfg.CONF)
    setup_logging(cfg.CONF)
    setup_privsep(cfg.CONF)
