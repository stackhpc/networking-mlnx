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

from networking_mlnx._i18n import _
from networking_mlnx.eswitchd.common import constants
from networking_mlnx.eswitchd.common import exceptions

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
    PCI_NET_PATH = ETH_DEV + "/virtfn%(vf_num)d/net"
    IB_DEV_PATH = "/sys/class/infiniband/%(ib_dev)s/device"
    VF_PCI_DEV_PATH = "/sys/bus/pci/devices/%(pf_pci_addr)s/virtfn%(vf_num)d"

    def get_vfs_info(self, pf):
        """Get VFs information

        :param pf: PF net device name
        :return: a dict containing VF info of the given PF
                 dict format example: {'04:00.3' : {'vf_num': 2,
                                                    'vf_device_type': 'MLNX5'
                                                   },
                                       ...
                                      }
        """
        vfs_info = {}
        try:
            dev_path = self.ETH_DEV % {'interface': pf}
            dev_info = os.listdir(dev_path)
            device_type = self.get_pf_device_type(pf)
            for dev_filename in dev_info:
                result = self._VIRTFN_RE.match(dev_filename)
                if result and result.group('vf_num'):
                    dev_file = os.path.join(dev_path, dev_filename)
                    vf_pci = os.readlink(dev_file).strip("./")
                    vf_num = int(result.group('vf_num'))
                    vf_device_type = device_type
                    vfs_info[vf_pci] = {'vf_num': vf_num,
                                        'vf_device_type': vf_device_type}
        except Exception as e:
            LOG.error("PCI device %s not found. %s", pf, str(e))
        LOG.info("VFs info for PF %s: %s", pf, vfs_info)
        return vfs_info

    def get_dev_attr(self, attr_path):
        try:
            with open(attr_path) as fd:
                return fd.readline().strip()
        except IOError:
            return

    def verify_vendor_pf(self, pf, vendor_id=constants.VENDOR):
        """Ensure PF net device PCI vendor ID equals vendor_id

        :param pf: PF netdev name
        :param vendor_id: PCI vendor ID
        :return: True if the PCI device id of the PF equals vendor_id
                 else false
        """
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
                raise Exception(_('driver type %s is not supported'),
                                driver_type)
        except IOError:
            pass
        return device_type

    def is_sriov_pf(self, pf):
        """Checks if PF net dev exists and SR-IOV is enabled

        :param pf: pf netdev name
        :return: True if the device exists and has SR-IOV enabled.
        """
        vfs_path = pciUtils.VFS_PATH % {'interface': pf}
        vfs = glob.glob(vfs_path)
        if vfs:
            return True
        else:
            return

    def get_pf_mlx_dev(self, pf):
        """Get PF Infiniband device (AKA mlx device)

        :param pf: pf netdev name
        :return: pf mlx device name
        """
        dev_path = (
            os.path.join(pciUtils.ETH_PATH % {'interface': pf},
            pciUtils.INFINIBAND_PATH))
        dev_info = os.listdir(dev_path)
        return dev_info.pop()

    def get_pci_from_ib_dev(self, ib_dev):
        """Get PF PCI address from IB device

        :param ib_dev: ib device
        :return: PCI address associated with the provided IB device
        :raises: DeviceNotFoundException
        """
        pf_dev_path = pciUtils.IB_DEV_PATH % {'ib_dev': ib_dev}
        if os.path.exists(pf_dev_path):
            return os.readlink(pf_dev_path).split(os.sep)[-1]
        raise exceptions.DeviceNotFoundException(
            "PCI address was not found for IB device %s" % ib_dev)

    def get_vf_from_vf_idx(self, pf_pci, vf_idx):
        """Get VF PCI address from PF PCI address and VF index

        :param pf_pci: PF PCI address D:B:D.F format
        :param vf_idx: VF index
        :return: VF PCI address
        :raises: DeviceNotFoundException
        """
        vf_dev_path = pciUtils.VF_PCI_DEV_PATH % {
            'pf_pci_addr': pf_pci, 'vf_num': vf_idx}
        if os.path.exists(vf_dev_path):
            return os.readlink(vf_dev_path).split(os.sep)[-1]
        raise exceptions.DeviceNotFoundException(
            "VF PCI address not found for PF %s VF index %d " % (
                pf_pci, vf_idx))

    def get_eth_port(self, dev):
        """Get network device Port number

        :param dev: netdev name
        :return: HCA port number
        """
        port_path = pciUtils.ETH_PORT % {'interface': dev}
        try:
            with open(port_path) as f:
                dev_id = int(f.read(), 0)
                return dev_id + 1
        except IOError:
            return

    def is_assigned_vf(self, pf_name, vf_index):
        """Check if VF is assigned.

       Checks if a given vf index of a given device name is assigned
       by checking the relevant path in the system:
       VF is assigned if PCI_PATH does not exist.
       @param pf_name: pf network device name
       @param vf_index: vf index
        """
        if not self.is_sriov_pf(pf_name):
            # If the root PCI path does not exist or has no VFs then
            # the VF cannot actually have been allocated and there is
            # no way we can manage it.
            return False

        path = self.PCI_NET_PATH % {'interface': pf_name, 'vf_num': vf_index}

        return not os.path.exists(path)
