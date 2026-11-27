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
   same GUID as on the source, keeps its link disabled, and remembers that
   Nova plugged it until ``eswitchd`` restarts. Nova then marks the port as
   migrating to the destination (``migrating_to`` in its binding profile).
#. The destination's ``neutron-mlnx-agent`` finds, within a polling
   interval, the virtual function that still has the GUID of a port bound to
   another host and migrating to this one, and reports the port up. Neutron
   only forwards this to Nova as the ``network-vif-plugged`` event that Nova
   waits for before it copies the guest: the port's status doesn't change,
   and the virtual function isn't configured: its link isn't enabled, or on
   ConnectX-3 adapters, whose link can't be controlled, its partition key
   isn't written. The agent reports each plug once; if Nova unplugs the
   virtual function, for example when it rolls back the migration, and
   plugs it again, the agent reports it again. If ``neutron-mlnx-agent``
   restarts during the migration, it may report it again, which does no
   harm. If ``eswitchd`` restarts
   before the agent finds the virtual function, nothing is reported, and
   Nova times out and rolls back the migration, which can be tried again.
   The agent stops looking at a plugged virtual function that matches no
   port migrating to its host after 10 minutes.
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

Leave ``[compute] live_migration_wait_for_vif_plug`` at its default,
``True``. Before it starts copying a guest, Nova then waits for a
``network-vif-plugged`` event from every port of the instance, which
``neutron-mlnx-agent`` and ``eswitchd`` on the destination host send for an
InfiniBand port once its virtual function is plugged there. Both must be
upgraded to a version that does this, and restarted, on every compute host
that can receive instances with InfiniBand ports: restart ``eswitchd``
before ``neutron-mlnx-agent``, or together with it. With an older
``eswitchd``, ``neutron-mlnx-agent`` logs a warning that it can't tell when
the virtual function is plugged, nothing sends the event, and every live
migration fails when ``[DEFAULT] vif_plugging_timeout`` expires. The agent
notices an upgraded ``eswitchd`` within a minute.

Earlier versions needed ``live_migration_wait_for_vif_plug = False`` on the
destination hosts. Once both services are upgraded, remove that setting, so
that Nova also waits for the instance's other ports, such as Open vSwitch or
OVN ones, to be set up on the destination before it copies the guest.

Leave ``[DEFAULT] vif_plugging_timeout`` and ``vif_plugging_is_fatal`` at
their defaults as well: setting the timeout to 0 would also stop Nova
waiting for ports when it boots instances.

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

Leave ``[DEFAULT] notify_nova_on_port_status_changes`` at its default,
``True``, in Neutron's configuration: Neutron only sends Nova the
``network-vif-plugged`` event for the destination host if it is set.

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

Nothing more is needed in ``local.conf`` than for booting instances with
InfiniBand ports. DevStack runs ``neutron-mlnx-agent`` and ``eswitchd`` from
the same checkout of networking-mlnx, so both send the event. Remove any
``live_migration_wait_for_vif_plug = False`` left in ``local.conf`` from
earlier versions.
