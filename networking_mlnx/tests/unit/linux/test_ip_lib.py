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

import mock

from neutron.privileged.agent.linux import ip_lib as n_privileged

from networking_mlnx.linux import constants
from networking_mlnx.linux import ip_lib
from networking_mlnx.tests import base


class TestIPoIBWrapper(base.TestCase):
    def setUp(self):
        super(TestIPoIBWrapper, self).setUp()
        self.execute_p = mock.patch.object(ip_lib.IPoIBWrapper, '_execute')
        self.execute = self.execute_p.start()
        self.namespace = "test-ns"
        self.ipoib = ip_lib.IPoIBWrapper(namespace=self.namespace)

    def test_segmentation_id_to_pkey_default(self):
        pkey = self.ipoib._segmentation_id_to_pkey(None)
        self.assertEqual(constants.DEFAULT_PKEY, pkey)
        pkey = self.ipoib._segmentation_id_to_pkey(0)
        self.assertEqual(constants.DEFAULT_PKEY, pkey)

    def test_segmentation_id_to_pkey_int_val(self):
        seg_id = 4
        pkey = self.ipoib._segmentation_id_to_pkey(seg_id)
        self.assertEqual(seg_id, pkey)

    def test_segmentation_id_to_pkey_str_val(self):
        seg_id = "14"
        pkey = self.ipoib._segmentation_id_to_pkey(seg_id)
        self.assertEqual(int(seg_id), pkey)

    def test_del_ipoib(self):
        with mock.patch.object(
                n_privileged, 'delete_interface') as del_ifc_mock:
            self.ipoib.del_ipoib("ipoib0")
            del_ifc_mock.assert_called_with("ipoib0", self.namespace)

    def test_add_ipoib(self):
        with mock.patch.object(
                n_privileged, 'create_interface') as create_ifc_mock, \
            mock.patch.object(
                n_privileged, 'set_link_attribute') as set_link_attr_mock:
            name = "ipoib0"
            src_dev = "ib0"
            pkey = 0x4
            ipdev = self.ipoib.add_ipoib(name, src_dev, pkey)
            # Assert device created in default namespace
            create_ifc_mock.assert_called_with(
                name, None, 'ipoib', physical_interface=src_dev, pkey=pkey)
            # Assert device moved to namespace
            set_link_attr_mock.assert_called_with(
                name, None, net_ns_fd=self.namespace)
            self.assertEqual(name, ipdev.name)
            self.assertEqual(self.namespace, ipdev.namespace)
