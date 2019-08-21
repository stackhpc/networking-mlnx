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

import copy

import mock
from oslo_concurrency import lockutils
from oslo_utils.fixture import uuidsentinel as uuids

from networking_mlnx.linux.interface_drivers import constants
from networking_mlnx.linux.interface_drivers import network_cache
from networking_mlnx.tests import base

network_db = {
    uuids.obj_a: {'id': uuids.obj_a, 'attrA': 'a-attr', 'attrB': 'b-attr'},
    uuids.obj_b: {'id': uuids.obj_b, 'attrA': 'a-attr', 'attrB': 'b-attr'},
    uuids.obj_c: {'id': uuids.obj_c, 'attrA': 'a-attr', 'attrB': 'b-attr'},
}


class TestSimpleCache(base.TestCase):
    def setUp(self):
        super(TestSimpleCache, self).setUp()
        self.cache = network_cache.SimpleCache()
        self._init_cache()

    def _init_cache(self):
        self.cache.object_cache = copy.deepcopy(network_db)

    def test_contains_operator(self):
        self.assertIn(uuids.obj_a, self.cache)
        self.assertNotIn(uuids.obj_non_existent, self.cache)
        self.assertNotIn(uuids.obj_non_existent, self.cache.object_cache)

    def test_get(self):
        obj = self.cache.get(uuids.obj_b)
        self.assertDictEqual(network_db[uuids.obj_b], obj)

    def test_get_non_existent_entry(self):
        self.assertIsNone(self.cache.get(uuids.obj_non_existent))

    def test_get_all(self):
        objs = self.cache.get_all()
        objs_ids = set([obj['id'] for obj in objs])
        self.assertEqual(objs_ids, set(network_db.keys()))
        for obj in objs:
            self.assertDictEqual(network_db[obj['id']], obj)

    def test_put(self):
        new_entry = {'id': uuids.new_obj, 'attrA': 'a-attr', 'attrB': 'b-attr'}
        self.cache.put(uuids.new_obj, new_entry)
        obj = self.cache.get(uuids.new_obj)
        self.assertDictEqual(new_entry, obj)

    def test_remove(self):
        self.cache.remove(uuids.obj_a)
        self.assertIsNone(self.cache.get(uuids.obj_a))
        self.assertNotIn(uuids.obj_a, self.cache.object_cache)

    def test_clear(self):
        self.cache.clear()
        self.assertListEqual([], self.cache.get_all())


class TestNetworkCache(base.TestCase):
    def _get_networks_cb(self, filters, fields):
        if 'id' in filters:
            net_id = filters["id"][0]
            nets = [self.network_db[net_id]]
        else:
            nets = list(self.network_db.values())
        return nets

    def setUp(self):
        super(TestNetworkCache, self).setUp()
        self.cb_mock = mock.Mock()
        self.cb_mock.side_effect = self._get_networks_cb
        self.network_fields = ['attrA', 'attrB', 'id']
        self.cache = network_cache.NetworkCache(
            self.cb_mock, self.network_fields)
        self.network_db = copy.deepcopy(network_db)

    def test_get(self):
        self.cache.put(uuids.obj_a, self.network_db[uuids.obj_a])
        obj = self.cache.get(uuids.obj_a)
        self.assertDictEqual(self.network_db[uuids.obj_a], obj)

    def test_get_with_miss(self):
        self.cache.put(uuids.obj_a, self.network_db[uuids.obj_a])
        obj = self.cache.get(uuids.obj_b)
        self.assertTrue(self.cb_mock.called)
        self.assertDictEqual(self.network_db[uuids.obj_b], obj)

    def test_refresh(self):
        self.cache.refresh()
        self.cb_mock.assert_called_once_with(
            filters={constants.ADMIN_STATE_UP: [True]},
            fields=self.network_fields)
        objs = self.cache.get_all()
        self.assertEqual(
            set(self.network_db.keys()), set([obj['id'] for obj in objs]))

    def test_remove_stale_networks(self):
        self.cache.put(uuids.obj_a, self.network_db[uuids.obj_a])
        self.cache.put(uuids.obj_b, self.network_db[uuids.obj_b])
        del self.network_db[uuids.obj_a]
        self.cache.remove_stale_networks()
        self.assertNotIn(uuids.obj_a, self.cache)
        self.assertIn(uuids.obj_b, self.cache)


class TestSafeNetworkCache(TestNetworkCache):
    def setUp(self):
        super(TestSafeNetworkCache, self).setUp()
        with mock.patch.object(lockutils, 'ReaderWriterLock') as rw_cls:
            self.rw_lock_mock = rw_cls.return_value = mock.MagicMock()
            self.cache = network_cache.SafeNetworkCache(
                self.cb_mock, ['attrA', 'attrB'])

    def test_get(self):
        self.cache.get(uuids.obj_a)
        self.assertTrue(self.rw_lock_mock.read_lock.called)

    def test_get_all(self):
        self.cache.get_all()
        self.assertTrue(self.rw_lock_mock.read_lock.called)

    def test_put(self):
        self.cache.put(uuids.new_obj_id, {})
        self.assertTrue(self.rw_lock_mock.write_lock.called)

    def test_remove(self):
        self.cache.remove(uuids.obj_a)
        self.assertTrue(self.rw_lock_mock.write_lock.called)

    def test_clear(self):
        self.cache.clear()
        self.assertTrue(self.rw_lock_mock.write_lock.called)

    def test_refresh(self):
        self.cache.refresh()
        self.assertTrue(self.rw_lock_mock.write_lock.called)

    def test_cache_miss_with_real_lock(self):
        # re-instantiate cache with a real lock
        self.cache = self.cache = network_cache.SafeNetworkCache(
                self.cb_mock, ['attrA', 'attrB'])
        self.test_get_with_miss()
