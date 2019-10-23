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
import glob
import os
import re

from oslo_log import log as logging

from networking_mlnx._i18n import _LE
from networking_mlnx.eswitchd.common import constants

LOG = logging.getLogger(__name__)


class pciUtils(object):

    ETH_PATH = "/sys/class/net/%(interface)s"
    ETH_DEV = ETH_PATH + "/device"
    ETH_DRIVER = ETH_DEV + "/driver"
    ETH_PORT = ETH_PATH + "/dev_id"
    INFINIBAND_PATH = 'device/infiniband'
    VENDOR_PATH = ETH_DEV + '/vendor'
    _VIRTFN_RE = re.compile(r'virtfn(?P<vf_num>\d+)')
    VFS_PATH = ETH_DEV + "/virtfn*"

    def get_vfs_info(self, pf):
        vfs_info = {}
        try:
            dev_path = self.ETH_DEV % {'interface': pf}
            dev_info = os.listdir(dev_path)
            for dev_filename in dev_info:
                result = self._VIRTFN_RE.match(dev_filename)
                if result and result.group('vf_num'):
                    dev_file = os.path.join(dev_path, dev_filename)
                    vf_pci = os.readlink(dev_file).strip("./")
                    vf_num = result.group('vf_num')
                    vf_device_type = self.get_pf_device_type(pf)
                    vfs_info[vf_pci] = {'vf_num': vf_num,
                                        'vf_device_type': vf_device_type}
        except Exception:
            LOG.error("PCI device %s not found", pf)
        return vfs_info

    def get_dev_attr(self, attr_path):
        try:
            fd = open(attr_path)
            return fd.readline().strip()
        except IOError:
            return

    def verify_vendor_pf(self, pf, vendor_id=constants.VENDOR):
        vendor_path = pciUtils.VENDOR_PATH % {'interface': pf}
        if self.get_dev_attr(vendor_path) == vendor_id:
            return True
        else:
            return False

    def get_pf_device_type(self, pf):
        device_type = None
        try:
            driver_type = os.readlink(self.ETH_DRIVER
                                      % {'interface': pf})
            driver_type = os.path.basename(driver_type)
            if driver_type == constants.MLNX4_DRIVER_TYPE:
                device_type = constants.MLNX4_DEVICE_TYPE
            elif driver_type == constants.MLNX5_DRIVER_TYPE:
                device_type = constants.MLNX5_DEVICE_TYPE
            else:
                raise Exception(_LE('driver type %s is not supported'),
                                driver_type)
        except IOError:
            pass
        return device_type

    def is_sriov_pf(self, pf):
        vfs_path = pciUtils.VFS_PATH % {'interface': pf}
        vfs = glob.glob(vfs_path)
        if vfs:
            return True
        else:
            return

    def get_pf_mlx_dev(self, pf):
        dev_path = (
            os.path.join(pciUtils.ETH_PATH % {'interface': pf},
            pciUtils.INFINIBAND_PATH))
        dev_info = os.listdir(dev_path)
        return dev_info.pop()

    def get_guid_index(self, pf_mlx_dev, dev, hca_port):
        guid_index = None
        path = constants.MLNX4_GUID_INDEX_PATH % (pf_mlx_dev, dev, hca_port)
        with open(path) as fd:
            guid_index = fd.readline().strip()
        return guid_index

    def get_eth_port(self, dev):
        port_path = pciUtils.ETH_PORT % {'interface': dev}
        try:
            with open(port_path) as f:
                dev_id = int(f.read(), 0)
                return dev_id + 1
        except IOError:
            return

    def get_vfs_macs_ib(self, fabric_details):
        macs_map = {}
        for pf_fabric_details in fabric_details.values():
            if (pf_fabric_details['pf_device_type'] ==
                constants.MLNX4_DEVICE_TYPE):
                macs_map.update(self.get_vfs_macs_ib_mlnx4(pf_fabric_details))
            elif (pf_fabric_details['pf_device_type'] ==
                  constants.MLNX5_DEVICE_TYPE):
                macs_map.update(self.get_vfs_macs_ib_mlnx5(pf_fabric_details))
        return macs_map

    def get_vfs_macs_ib_mlnx4(self, fabric_details):
        hca_port = fabric_details['hca_port']
        pf_mlx_dev = fabric_details['pf_mlx_dev']
        macs_map = {}
        guids_path = constants.MLNX4_ADMIN_GUID_PATH % (pf_mlx_dev, hca_port,
                                                  '[1-9]*')
        paths = glob.glob(guids_path)
        for path in paths:
            vf_index = path.split('/')[-1]
            with open(path) as f:
                guid = f.readline().strip()
                if guid == constants.MLNX4_INVALID_GUID:
                    mac = constants.INVALID_MAC
                else:
                    head = guid[:6]
                    tail = guid[-6:]
                    mac = ":".join(re.findall('..?', head + tail))
                macs_map[str(int(vf_index))] = mac
        return macs_map

    def get_vfs_macs_ib_mlnx5(self, fabric_details):
        vfs = fabric_details['vfs']
        macs_map = {}
        for vf in vfs.values():
            vf_num = vf['vf_num']
            pf_mlx_dev = fabric_details['pf_mlx_dev']
            guid_path = (
                constants.MLNX5_GUID_NODE_PATH % {'module': pf_mlx_dev,
                                                  'vf_num': vf_num})
            with open(guid_path) as f:
                guid = f.readline().strip()
                head = guid[:8]
                tail = guid[-9:]
                mac = head + tail
            macs_map[vf_num] = mac
        return macs_map

    def get_device_address(self, hostdev):
        domain = hostdev.attrib['domain'][2:]
        bus = hostdev.attrib['bus'][2:]
        slot = hostdev.attrib['slot'][2:]
        function = hostdev.attrib['function'][2:]
        dev = "%.4s:%.2s:%2s.%.1s" % (domain, bus, slot, function)
        return dev
