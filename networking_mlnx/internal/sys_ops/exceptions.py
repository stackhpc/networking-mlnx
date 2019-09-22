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

from networking_mlnx._i18n import _


class SystemOperationError(RuntimeError):
    message = _("Failed to perform system operation. %(e)s")

    def __init__(self, e):
        message = self.message % {'e': str(e)}
        super(SystemOperationError, self).__init__(message)


class InvalidSystemPathError(RuntimeError):
    message = _("Invalid System path provided. %(path)s")

    def __init__(self, path):
        message = self.message % {'path': path}
        super(InvalidSystemPathError, self).__init__(message)
