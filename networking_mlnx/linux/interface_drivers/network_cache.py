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

from oslo_concurrency import lockutils
from oslo_log import log

from networking_mlnx.linux.interface_drivers import constants

LOG = log.getLogger(__name__)


class SimpleCache(object):
    """A simple object cache"""

    def __init__(self):
        self.object_cache = {}

    def __contains__(self, id):
        return id in self.object_cache

    def get(self, id):
        """Get object from cache

        :param id: object id
        :return: object or None if id is not in cache
        """
        return self.object_cache.get(id)

    def get_all(self):
        """Get all object from cache

        :return: list of objects
        """
        return list(self.object_cache.values())

    def put(self, id, obj):
        """Add object to cache

        :param id: object id
        :param obj: object to add
        """
        self.object_cache[id] = obj

    def remove(self, id):
        """Remove object from cache

        :param id: object id
        """
        if id in self:
            del self.object_cache[id]

    def clear(self):
        """Clear the cache"""
        self.object_cache = {}


class NetworkCache(SimpleCache):
    """A network cache that retrieves missing entries
    via callback.
    """

    def __init__(self, get_networks_cb, fields):
        """__init__

        :param get_networks_cb: a callback to get network object
               the callback should accept filters and fields parameters.
               e.g: def get_networks(filters, fields)
        :param fields: a list of fields to get for a network object
                       used as an argument for get_networks_cb when getting
                       new network objects. None value will fetch all available
                       fields.
        """
        super(NetworkCache, self).__init__()
        self.get_networks_cb = get_networks_cb
        self.network_fields = fields
        if self.network_fields and 'id' not in self.network_fields:
            self.network_fields.append('id')

    def _get_no_callback(self, network_id):
        """Get network from cache
        in case of a cache miss, return None.

        :param network_id: network id
        :return: network dict or None
        """
        return super(NetworkCache, self).get(network_id)

    def get(self, network_id):
        """Get network from cache
        in case of a cache miss, attempt to get network from callback.

        :param network_id: network id
        :return: network dict
        """
        net = self._get_no_callback(network_id)
        if net is None:
            LOG.debug("Network %s not in cache, fetching via callback.",
                      network_id)
            net = self.get_networks_cb(filters={'id': [network_id]},
                                       fields=self.network_fields)[0]
            self.put(net['id'], net)
        return net

    def refresh(self):
        """Refresh network cache"""
        nets = self.get_networks_cb(filters={constants.ADMIN_STATE_UP: [True]},
                                    fields=self.network_fields)
        self.clear()
        for net in nets:
            self.put(net['id'], net)

    def remove_stale_networks(self):
        """Remove networks that no longer exist or are inactive from cache

        This method is intended to be used as a periodic job to prevent the
        cache from containing too many stale entries.
        """
        nets = self.get_networks_cb(filters={constants.ADMIN_STATE_UP: [True]},
                                    fields=['id'])
        current_net_ids = set([net['id'] for net in nets])
        cached_net_ids = set([net['id'] for net in self.get_all()])
        ids_to_remove = cached_net_ids - current_net_ids
        if ids_to_remove:
            LOG.debug("Removing stale networks from cache: %s", ids_to_remove)
            for id in ids_to_remove:
                self.remove(id)


class SafeNetworkCache(NetworkCache):
    """Thread safe implementation of NetworkCache
    Which basically wraps operations with a ReaderWriter Lock
    """

    def __init__(self, get_networks_cb, fields):
        """__init__

        :param get_networks_cb: a callback to get network object
               the callback should accept filters and fields parameters.
               e.g: def get_networks(filters, fields)
        :param fields: a list of fields to get for a network object
                       used as an argument for get_networks_cb when getting
                       new network objects. None value will fetch all available
                       fields.
        """
        super(SafeNetworkCache, self).__init__(get_networks_cb, fields)
        self.__rw_lock = lockutils.ReaderWriterLock()

    def get(self, id):
        """Get network from cache
        in case of a cache miss, attempt to get network from callback.

        :param id: network id
        :return: network dict
        """
        with self.__rw_lock.read_lock():
            net = self._get_no_callback(id)

        if net is None:
            LOG.debug("Network %s not in cache, fetching via callback.", id)
            net = self.get_networks_cb(filters={'id': [id]},
                                       fields=self.network_fields)[0]
            # Put operation is already protected by a writer lock
            self.put(net['id'], net)
        return net

    def get_all(self):
        """Get all object from cache

        :return: list of objects
        """
        with self.__rw_lock.read_lock():
            return super(SafeNetworkCache, self).get_all()

    def put(self, id, obj):
        """Add object to cache

        :param id: object id
        :param obj: object to add
        """
        with self.__rw_lock.write_lock():
            return super(SafeNetworkCache, self).put(id, obj)

    def remove(self, id):
        """Remove object from cache

        :param id: object id
        """
        with self.__rw_lock.write_lock():
            return super(SafeNetworkCache, self).remove(id)

    def clear(self):
        """Clear the cache"""
        with self.__rw_lock.write_lock():
            return super(SafeNetworkCache, self).clear()

    def refresh(self):
        """Refresh network cache"""
        with self.__rw_lock.write_lock():
            return super(SafeNetworkCache, self).refresh()
