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

import os

from oslo_log import log as logging

from networking_mlnx.internal.sys_ops import exceptions
from networking_mlnx.internal.sys_ops import sys_ops_abs

LOG = logging.getLogger(__name__)


class OsCommand(sys_ops_abs.SystemOps):

    SYS_BASE_PATH = os.path.join(os.sep, "sys", os.sep)

    def sys_write(self, path, value):
        """Write to sysfs path (/sys/...)

        :param path: sysfs path
        :param value: value to write
        :raises InvalidSystemPathError: provided path is not under sysfs
        :raises SystemOperationError: an error occured when opening/writing
                                      to file.
        """
        if not os.path.realpath(path).startswith(OsCommand.SYS_BASE_PATH):
            raise exceptions.InvalidSystemPathError(path)
        try:
            with open(path, 'w') as fd:
                fd.write("%s" % value)
        except Exception as e:
            raise exceptions.SystemOperationError(e)
