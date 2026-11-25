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

from neutron_lib import exceptions as exc

from networking_mlnx._i18n import _


class SDNConnectionError(exc.NeutronException):
    message = _("Failed to send request %(msg)s")


class SDNUnreachableError(SDNConnectionError):
    """The request got no answer: the controller may not have seen it."""
    message = _("SDN controller unreachable: %(msg)s")


class SDNRequestError(exc.NeutronException):
    message = _("Request rejected by the SDN controller: %(msg)s")


class SDNLoginError(exc.NeutronException):
    message = _("Failed login to URL: %(login_url)s %(msg)s")


class SDNDriverConfError(exc.NeutronException):
    message = _("Driver configuration error: %(msg)s")


class SDNDriverCertError(exc.NeutronException):
    message = _("SSL certificate error: %(msg)s")


class JournalRowNotFound(exc.NotFound):
    message = _("Journal row %(row_id)s does not exist")


class JournalRowNotFailed(exc.Conflict):
    message = _("Journal row %(row_id)s is %(state)s, not failed")


class JournalRowNotRetryable(exc.Conflict):
    message = _("Journal row %(row_id)s can't be sent again: %(reason)s")


class JournalRowSuperseded(exc.Conflict):
    message = _("Journal row %(row_id)s can't be sent again: row "
                "%(newer_id)s, a later %(operation)s of the same object, "
                "would be undone")
