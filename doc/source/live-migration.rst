==============
Live migration
==============

Nova can live migrate an instance with an InfiniBand port of networking-mlnx
(VNIC type ``direct``, VIF type ``ib_hostdev``) between compute hosts on the
same InfiniBand fabric. The instance keeps its memory and disks, but its
virtual function is swapped for one on the destination host.

This has not yet been tested on hardware with two compute hosts.

How it works
------------

#. Nova creates an inactive binding of the port on the destination host and
   claims a virtual function there. ``mlnx_infiniband`` binds it if the
   destination's ``neutron-mlnx-agent`` is alive and maps the port's physical
   network. Nothing is sent to an SDN controller yet.
#. Nova plugs the destination virtual function with ``ebrctl add-port``:
   ``eswitchd`` gives it the GUID derived from the port's MAC address, the
   same GUID as on the source, and keeps its link disabled. The virtual
   function isn't attached to the guest yet, so ``neutron-mlnx-agent`` doesn't
   see it, and nothing sends Nova a ``network-vif-plugged`` event for the
   port.
#. Nova hot-unplugs the virtual function from the guest on the source host.
   ``eswitchd`` invalidates its GUID and disables its link.
#. The guest's memory is copied to the destination while it runs without its
   InfiniBand interface.
#. Nova activates the destination binding, and the port goes ``DOWN`` until
   the destination's ``neutron-mlnx-agent`` reports it up. With
   ``mlnx_sdn_assist`` syncing a VLAN network, the SDN controller is asked to
   delete the port for the source host and then to create it for the
   destination host, and the port also stays ``DOWN`` until the controller has
   added it to its partition key.
#. Nova hot-plugs the destination virtual function into the guest.
   ``neutron-mlnx-agent`` reports the port up, and enables the virtual
   function's link once the port is ``ACTIVE``. On ConnectX-3 adapters, whose
   link can't be controlled, ``eswitchd`` writes the virtual function's
   partition key instead.

What the guest sees
-------------------

* The InfiniBand device disappears from the start of the memory copy until
  the port is ``ACTIVE`` on the destination host: usually seconds to a
  minute, longer for busy guests with a lot of memory, and longer while an
  SDN controller adds the port to its partition key.
* It comes back as a new PCI device with the same GUID, possibly with another
  interface name. The guest only gets its IPoIB address back if it runs DHCP
  on the new interface, or applies its static configuration to it, so don't
  tie a NetworkManager profile or udev rule to the old PCI path or name.
* RDMA connections, and IPoIB connections in connected mode, are broken and
  must be established again. Quiesce RDMA work before a migration.
* Other ports, such as one on a tenant network, stay attached and migrate as
  usual.

Requirements
------------

Nova
~~~~

Set this on every compute host that can receive instances with InfiniBand
ports:

.. code-block:: ini

   [compute]
   live_migration_wait_for_vif_plug = False

Before it starts copying a guest, Nova waits for a ``network-vif-plugged``
event from every port of the instance, if this option is ``True`` (its
default) on the destination host. Nothing sends that event for an InfiniBand
port before the copy, so with the default every live migration fails when
``[DEFAULT] vif_plugging_timeout`` expires. Nova reads the option on the
destination host. With it set to ``False``, Nova also doesn't wait for the
instance's other ports, such as Open vSwitch or OVN ones, to be set up on the
destination, so they may briefly lose packets after the migration.

Leave ``[DEFAULT] vif_plugging_timeout`` and ``vif_plugging_is_fatal`` at
their defaults: setting the timeout to 0 instead would also stop Nova waiting
for ports when it boots instances.

Nova also needs, as for booting instances with InfiniBand ports:

* the virtual functions in ``[pci] device_spec`` on each compute host, with
  their ``physical_network``, and ``PciPassthroughFilter`` in
  ``[filter_scheduler] enabled_filters``;
* a free virtual function on the same physical network on the destination
  host, which also meets the instance's NUMA policy; otherwise Nova refuses
  the migration.

The rest is as for any live migration: compatible CPUs and ``[libvirt]
cpu_mode``, a migration network, shared instance storage or block migration
(``openstack server migrate --live-migration --block-migration``), and a
``[libvirt] live_migration_completion_timeout`` that allows for the guest
running without InfiniBand for the whole copy. See Nova's `Configure live
migrations
<https://docs.openstack.org/nova/latest/admin/configuring-migrations.html>`_.

Neutron
~~~~~~~

Nova only live migrates an instance with a ``direct`` port if Neutron has the
``binding-extended`` extension, which ML2 provides. ``neutron-mlnx-agent`` and
``eswitchd`` must run on both hosts and map the port's physical network.

Guests
~~~~~~

The guest's operating system must release the virtual function when Nova
hot-unplugs it. If it doesn't within ``[libvirt] device_detach_timeout`` and
``device_detach_attempts``, Nova aborts the migration before copying
anything, and the instance stays on the source host. But Nova has already
unplugged the virtual function, which invalidates its GUID, so the guest
keeps the device without InfiniBand connectivity until the instance is hard
rebooted.

DevStack
--------

Add this to ``local.conf`` on every host that runs ``nova-compute``:

.. code-block:: ini

   [[post-config|$NOVA_CPU_CONF]]
   [compute]
   live_migration_wait_for_vif_plug = False
