===============
DevStack plugin
===============

networking-mlnx includes a DevStack plugin. It installs networking-mlnx and
can run these services:

``mlnx-agt``
  The ``neutron-mlnx-agent`` L2 agent, used by the ``mlnx_infiniband``
  mechanism driver.

``eswitchd``
  The ``eswitchd`` daemon, which programs InfiniBand SR-IOV virtual functions
  (VFs) for the agent and for ``ebrctl``, which nova-compute calls when it
  plugs an ``ib_hostdev`` VIF.

The plugin's scripts expect to be checked out at ``$DEST/neutron_ml2_mlnx``,
so it must be enabled with the name ``neutron_ml2_mlnx``.

InfiniBand SR-IOV with OVN
==========================

This section sets up the ``mlnx_infiniband`` mechanism driver alongside
DevStack's default OVN backend. Instances keep their normal OVN ports and can
also have ``direct`` ports on an InfiniBand provider network, which Nova backs
with a VF passed through from the host's InfiniBand adapter.

It was tested on Rocky Linux 10.1 with a ConnectX-5 adapter.

Host requirements
-----------------

Set these up before running ``stack.sh``:

* A Mellanox/NVIDIA InfiniBand adapter with SR-IOV enabled in its firmware.
* The IOMMU enabled on the kernel command line, for example
  ``intel_iommu=on iommu=pt``.
* The ``ib_ipoib`` module loaded, so that the adapter's physical function has
  a network interface (``ibs1`` in the examples below).
* VFs created on that interface, for example::

    echo 8 | sudo tee /sys/class/net/ibs1/device/sriov_numvfs

  ``eswitchd`` exits at startup if the interface has no VFs. The setting does
  not persist across reboots.
* A subnet manager that supports virtualisation running on the fabric, such
  as MLNX OpenSM with ``virt_enabled 2`` (the default in MLNX OpenSM 5.17).
  Check that the port state is ``ACTIVE``::

    cat /sys/class/net/ibs1/device/infiniband/*/ports/*/state

local.conf
----------

Add the following to ``local.conf``. Replace ``ibphysnet`` and ``ibs1`` with
your physical network name and InfiniBand interface::

    [[local|localrc]]
    enable_plugin neutron_ml2_mlnx https://opendev.org/x/networking-mlnx
    enable_service mlnx-agt eswitchd
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=ovn,mlnx_infiniband
    PHYSICAL_INTERFACE_MAPPINGS=ibphysnet:ibs1
    Q_ML2_PLUGIN_FLAT_TYPE_OPTIONS=flat_networks=public,ibphysnet

    [[post-config|$NEUTRON_CONF]]
    [DEFAULT]
    global_physnet_mtu = 2044

    [[post-config|/$Q_PLUGIN_CONF_FILE]]
    [ml2]
    physical_network_mtus = public:1500,ibphysnet:2044
    path_mtu = 1500

    [[post-config|$NOVA_CONF]]
    [filter_scheduler]
    enabled_filters = ComputeFilter,ComputeCapabilitiesFilter,ImagePropertiesFilter,ServerGroupAntiAffinityFilter,ServerGroupAffinityFilter,SameHostFilter,DifferentHostFilter,PciPassthroughFilter

    [[post-config|$NOVA_CPU_CONF]]
    [pci]
    device_spec = {\\\"physical_network\\\": \\\"ibphysnet\\\", \\\"devname\\\": \\\"ibs1\\\"}

Don't enable ``q-dhcp`` or ``q-l3`` with this configuration: the plugin
configures them for IPoIB using ``PHYSICAL_NETWORK`` and
``PHYSICAL_INTERFACE``.

The settings do the following:

``PHYSICAL_INTERFACE_MAPPINGS``
  Maps the InfiniBand physical network to the physical function's interface,
  for both ``neutron-mlnx-agent`` and ``eswitchd``. Use this rather than
  ``PHYSICAL_NETWORK`` and ``PHYSICAL_INTERFACE``: with OVN, DevStack uses
  ``PHYSICAL_NETWORK`` (``public`` by default) for the external network on
  ``br-ex``.

``Q_ML2_PLUGIN_FLAT_TYPE_OPTIONS``
  Allows flat networks on the InfiniBand physical network as well as on
  ``public``.

``global_physnet_mtu``, ``physical_network_mtus`` and ``path_mtu``
  Allow an MTU of 2044, the IPoIB datagram mode MTU for a 2048 byte
  InfiniBand MTU. Use the MTU of your interface (``ip link show ibs1``).
  Neutron limits each physical network to the lower of
  ``global_physnet_mtu`` and its ``physical_network_mtus`` entry, so the
  global limit has to be raised. ``public`` stays at 1500, and ``path_mtu``
  keeps Geneve tenant networks at their usual MTU of 1442.

``enabled_filters``
  Adds ``PciPassthroughFilter``, so that instances with ``direct`` ports are
  scheduled to hosts with free VFs. This option replaces DevStack's default
  filter list, so the whole list has to be given.

``device_spec``
  Lets Nova pass the VFs of ``ibs1`` through to instances. The value must be
  JSON, but DevStack's ``merge_config_file`` reads each setting with ``read``
  (without ``-r``), which removes one level of backslashes, and then
  ``eval``\ s it inside double quotes. Each ``"`` therefore has to be written
  as ``\\\"``. Check the result in ``/etc/nova/nova-cpu.conf``::

    [pci]
    device_spec = {"physical_network": "ibphysnet", "devname": "ibs1"}

Testing
-------

After ``stack.sh`` finishes, check that the ``Mellanox plugin agent``
(``neutron-mlnx-agent``) is listed and alive::

    openstack network agent list

As an admin, create an InfiniBand network and a ``direct`` port, then boot an
instance with it. Port security is disabled because ``mlnx_infiniband`` does
not apply security groups::

    openstack network create infiniband \
        --provider-network-type flat --provider-physical-network ibphysnet \
        --mtu 2044 --disable-port-security
    openstack subnet create infiniband --network infiniband \
        --subnet-range 172.26.0.0/24 --no-dhcp --gateway none
    openstack port create ib-port --network infiniband --vnic-type direct
    openstack server create ib-test --flavor m1.tiny \
        --image cirros-0.6.3-x86_64-disk --network private --port ib-port \
        --wait

The port should be ``ACTIVE`` with ``binding_vif_type`` ``ib_hostdev``, and its
``binding_profile`` shows the VF's ``pci_slot``::

    openstack port show ib-port -c status -c binding_vif_type -c binding_profile

CirrOS sees the VF as a PCI device (``[15b3:1018]`` for a ConnectX-5 VF in
``openstack console log show ib-test``), but has no InfiniBand drivers. To
use the VF from the guest, use an image with the ``mlx5_ib`` and ``ib_ipoib``
kernel modules and ``rdma-core``.

cloud-init 24.4 can fail with ``Unable to find a system nic`` when an instance
boots with the InfiniBand port attached, because the port's MAC address in
``network_data.json`` does not match the IPoIB interface's hardware address.
CirrOS does not use cloud-init. With cloud-init images, boot without the port
and attach it after the first boot::

    openstack server add port ib-test ib-port

Tempest tests
-------------

The `networking-mlnx-tempest-plugin`_ tests this setup automatically. To
install it, clone it before running ``stack.sh``::

    git clone https://github.com/stackhpc/networking-mlnx-tempest-plugin \
        /opt/stack/networking-mlnx-tempest-plugin

Then add it to ``TEMPEST_PLUGINS`` in ``local.conf``. This is a
space-separated list, so append to it if you already install other plugins::

    [[local|localrc]]
    TEMPEST_PLUGINS=/opt/stack/networking-mlnx-tempest-plugin

When ``tempest`` and ``mlnx-agt`` are enabled, the DevStack plugin sets
``[service_available] networking_mlnx`` and ``[networking_mlnx]
physical_network`` in ``tempest.conf``. It takes the physical network from the
first entry of ``PHYSICAL_INTERFACE_MAPPINGS``, or from ``PHYSICAL_NETWORK``
if only ``PHYSICAL_NETWORK`` and ``PHYSICAL_INTERFACE`` are set. If neither is
set, it sets nothing and the tests are skipped. To change these or set the
plugin's other options, add them to a ``[[test-config|$TEMPEST_CONFIG]]``
section in ``local.conf``, which DevStack applies afterwards. Do this on
multi-node setups where Tempest runs on a host without ``mlnx-agt``, because
the DevStack plugin only sets the options on hosts that run it.

Run the tests in DevStack's Tempest environment::

    cd /opt/stack/tempest
    tox -e venv-tempest -- tempest run --regex networking_mlnx_tempest_plugin

The tests create their own flat network on the InfiniBand physical network,
and Neutron allows only one. They also need two free VFs. So first delete the
resources from the previous section::

    openstack server delete --wait ib-test
    openstack port delete ib-port
    openstack network delete infiniband

Alternatively, keep the network: share it with ``openstack network set
--share infiniband``, and set ``network_id`` to its ID in the ``[networking_mlnx]``
section of a ``[[test-config|$TEMPEST_CONFIG]]`` block. The server still has
to be deleted to free its VF.

.. _networking-mlnx-tempest-plugin: https://github.com/stackhpc/networking-mlnx-tempest-plugin

SDN mechanism driver
====================

To use the ``mlnx_sdn_assist`` mechanism driver with a Mellanox SDN
controller, add the following to ``local.conf``. These settings have not been
tested with a current DevStack::

    [[local|localrc]]
    enable_plugin neutron_ml2_mlnx https://opendev.org/x/networking-mlnx
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=ovn,mlnx_sdn_assist

    [[post-config|/$Q_PLUGIN_CONF_FILE]]
    [sdn]
    url = http://<sdn_provider_ip>/ufmRestV3
    domain = cloudx
    token = abcdef

Partition keys
--------------

networking-mlnx keeps an InfiniBand port off the fabric until the port is in
its network's partition key (pkey):

* With ``mlnx_sdn_assist`` enabled, the SDN controller adds a ``direct`` port
  (VIF type ``ib_hostdev``), or a bare metal node's InfiniBand port (with a
  DHCP client ID), on a VLAN network to the network's partition key. The port
  stays ``DOWN`` until the controller's job has completed, and is only sent
  once no deletion of a port with the same GUID, recorded no later than it,
  is pending. If the
  controller fails, or failed to delete a port with the same GUID, the port is
  set to ``ERROR``. Only administrators and services can set a port's DHCP
  client ID on a network that ``mlnx_sdn_assist`` syncs, since the controller
  takes the GUID from it. A port with more than one client ID can't be bound,
  nor can a ``direct`` port whose client ID isn't the one derived from its MAC
  address, since its virtual function's GUID is.
* On ConnectX-4 and later adapters, ``neutron-mlnx-agent`` enables a virtual
  function's link only while its port is ``ACTIVE``, administratively up and
  bound to the agent's host, and only if the virtual function has the GUID
  derived from the port's MAC address (comparing the 48 bits that the MAC
  address gives). It checks this on every polling iteration, so enforcement
  can lag by up to one interval, and disables the link otherwise. If it
  can't reach the Neutron server, it keeps only the links it enabled for the
  same port, and disables every link after it restarts. A disabled link takes the port in
  the guest down, so this holds however Nova uses the port, including when it
  attaches an interface, migrates or resumes an instance, or doesn't wait for
  the port. On ConnectX-3 adapters the link can't be controlled; instead
  ``eswitchd`` writes the virtual function's partition key, and leaves it in
  no partition if that fails.
* A bare metal node is protected by Ironic: it waits for the node's ports to
  be ``ACTIVE`` before it powers the node on into the tenant network, and
  fails the deployment otherwise.
* ``mlnx_infiniband`` doesn't bind a port to a VLAN segment unless
  ``mlnx_sdn_assist`` syncs the physical networks of all the network's
  segments. It doesn't bind a port to any segment if another port with the
  same MAC address, and so the same GUID, is bound as ``ib_hostdev``.

``mlnx_sdn_assist`` only sends a network's ports while every segment of the
network is on a physical network that it syncs, so it doesn't let a network
have segments both on synced physical networks and on others. Networks that
already mix them, from before this check, aren't synced; their ports were
never sent, or stopped being sent, so check for them after upgrading.

Ports on flat networks use the default partition, so all flat networks share
it.

What the deployment has to provide:

* ``mlnx_sdn_assist`` with ``[sdn] sync_enabled = True``, and VLAN networks'
  physical networks in ``[sdn] physical_networks``. Without them, set
  ``[mlnx] allow_unenforced_pkeys = True`` only if the subnet manager's
  partitions match the networks' segmentation IDs by other means.
* The subnet manager must give virtual functions, hypervisors and bare metal
  nodes only limited membership of the default partition. A port is added to
  its network's partition key, but nothing removes it from the default
  partition, so with full membership ports on different networks can reach
  each other there.
* A kernel that reports the GUID of a virtual function while it is assigned to
  a guest, as ``ip link`` shows. Otherwise no link is ever enabled.
* For Ironic, the ``neutron`` network interface and
  ``[neutron] fail_on_port_binding_failure = True``, the default, not
  overridden by a node's ``driver_info['fail_on_binding_failure']``. A port in
  ``ERROR`` fails the deployment when Ironic's wait for the port times out,
  unless Ironic fails early on ``ERROR`` ports.

A port only becomes ``ACTIVE``, and so an instance only gets InfiniBand
access, once nothing else holds it either, including a DHCP agent that hosts
the network. A port in ``ERROR`` stays held. To retry, unbind and rebind it,
for example by detaching it from the instance and attaching it again.

A deletion that the controller keeps rejecting is retried, with a growing delay
between attempts, and holds every port with the same GUID meanwhile. With the
default ``[sdn] retry_count`` of -1 it is retried until it succeeds. One that
runs out of retries, or whose job fails, stays in the
``sdn_journal`` table in the ``failed`` state, and every port with the same
GUID is set to ``ERROR``, until an operator has checked that the GUID was
removed from the partition key and deleted the row. This applies to every
failed port deletion, including Ethernet ports, whose MAC address is compared
with the GUIDs of instances' ports. Deletions recorded in the same second can
still wait for each other, which needs the same operator action.
