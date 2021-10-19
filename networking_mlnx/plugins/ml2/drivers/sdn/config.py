# Copyright 2016 Mellanox Technologies, Ltd
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

from oslo_config import cfg

from networking_mlnx._i18n import _
from networking_mlnx.plugins.ml2.drivers.sdn import constants as sdn_const

sdn_opts = [
        cfg.BoolOpt('sync_enabled',
                    help=_("Whether synchronising state to an SDN provider is "
                           "enabled."),
                    default=True),
        cfg.StrOpt('url',
                   help=_("HTTP URL of SDN Provider."),
                   ),
        cfg.StrOpt('domain',
                   help=_("Cloud domain name in SDN provider "
                          "(for example: cloudx)"),
                   default='cloudx'
                   ),
        cfg.StrOpt('token',
                   help=_("HTTPS token for authentication."),
                   secret=True,
                   default="abcdef",
                   ),
        cfg.IntOpt('timeout',
                   help=_("HTTP timeout in seconds."),
                   default=10
                   ),
        cfg.IntOpt('sync_timeout', default=10,
                   help=_("Sync thread timeout in seconds.")),
        cfg.IntOpt('retry_count', default=-1,
                   help=_("Number of times to retry a row "
                          "before failing."
                          "To disable retry count value should be -1")),
        cfg.IntOpt('maintenance_interval', default=300,
                   help=_("Journal maintenance operations interval "
                          "in seconds.")),
        cfg.IntOpt('completed_rows_retention', default=600,
                   help=_("Time to keep completed rows in seconds."
                          "Completed rows retention will be checked every "
                          "maintenance_interval by the cleanup thread."
                          "To disable completed rows deletion "
                          "value should be -1")),
        cfg.IntOpt('processing_timeout', default='100',
                   help=_("Time in seconds to wait before a "
                          "processing row is marked back to pending.")),
        cfg.ListOpt('physical_networks',
                    default=sdn_const.ANY,
                    help=_("Comma-separated list of <physical_network> "
                           "that it will send notification. * "
                           "means all physical_networks")),
        cfg.BoolOpt('bind_normal_ports',
                    default=False,
                    help=_("Allow the binding of normal ports for ports "
                           "associated with an InfiniBand physnet from "
                           "bind_normal_ports_physnets.")),
        # TODO(adrianc): The name here is a bit missleading, since there is no
        # usecase where SDN mechanism driver should bind normal ports for ETH
        # physnets. this should be renamed to: `infiniband_physnets` which will
        # require updates to deployment projects as well.
        cfg.ListOpt('bind_normal_ports_physnets',
                    default=[],
                    help=_("A list of InfiniBand physnets in which binding of "
                           "normal ports is allowed. This option is used in "
                           "conjuction with bind_normal_ports. "
                           "The list must be a subset of physical_networks")),
        cfg.BoolOpt('cert_verify',
                    default="False",
                    help=_("Use certificates to verify connections.")),
        cfg.StrOpt('cert_path',
                   default="",
                   help=_("The path of the certificate CA, if left empty the "
                          "connection will be verified by known trusted "
                          "authorities (mostly used when the certificates are "
                          "self signed).")),
]
