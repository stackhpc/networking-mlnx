==============
Partition keys
==============

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

The controller identifies a port by the GUID in its DHCP client ID, so
``mlnx_sdn_assist`` only sends it bound ports that have one, and fails the
request that would bind a ``direct`` port without one on a synced VLAN
network, so the port stays unbound. Other
ports, such as Ethernet ports or a network's DHCP and router ports, aren't
sent. A port's client ID can't be changed while it is bound to a synced
network; unbind the port first.

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
* ``neutron-periodic-workers`` and ``neutron-rpc-server``, with the same
  configuration as ``neutron-server``. Only ``neutron-periodic-workers``
  sends networks and ports to the SDN controller, every ``[sdn]
  sync_timeout`` seconds, and it asks ``neutron-rpc-server`` to make a port
  ``ACTIVE``, or ``ERROR``, once the controller has handled it, so
  ``[DEFAULT] rpc_workers`` must not be ``0``. Without either, ports held
  ``DOWN`` stay ``DOWN``. If no RPC worker answers, the journal logs an
  error, and tries again at its next sync to make the ports that the
  controller has added ``ACTIVE``. Ports that the controller failed to add
  stay ``DOWN`` rather than ``ERROR``.
* The subnet manager must give virtual functions, hypervisors and bare metal
  nodes only limited membership of the default partition. A port is added to
  its network's partition key, but nothing removes it from the default
  partition, so with full membership ports on different networks can reach
  each other there. A port can't leave the default partition altogether:
  its path record queries and multicast joins go to the subnet manager on the
  default partition key, and IPoIB and RDMA connections need them. Limited
  members can only reach full members, not each other, so make only the
  subnet manager's hosts full members. Any other full member can reach every
  port on the default partition.
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
GUID is set to ``ERROR``, until an operator deals with the row. This applies
to every failed port deletion, including any left from earlier releases for
Ethernet ports, whose MAC address is compared with the GUIDs of instances'
ports. Deletions recorded in the same second can still wait for each other,
which needs the same operator action.

Failed journal rows
~~~~~~~~~~~~~~~~~~~

The ``neutron-mlnx-manage journal`` commands show and recover the journal's
rows. Run them where ``neutron-server`` runs, with its configuration files, so
that they can reach Neutron's database. List the failed rows, and show one.
For a failed port deletion, ``show`` lists the other ports with the same GUID,
which can't be added to a partition key until it is dealt with::

    neutron-mlnx-manage --config-file /etc/neutron/neutron.conf \
        journal list --state failed
    neutron-mlnx-manage --config-file /etc/neutron/neutron.conf \
        journal show ROW_ID

As with the ``openstack`` client, ``list`` and ``show`` print a table, and
take ``-f json``, ``-f yaml`` or ``-f value``, and ``-c`` to choose columns.
For example, to list the IDs of the failed rows::

    neutron-mlnx-manage --config-file /etc/neutron/neutron.conf \
        journal list --state failed -f value -c ID

To recover a failed port deletion, once the controller is working again,
send it again::

    neutron-mlnx-manage --config-file /etc/neutron/neutron.conf \
        journal retry ROW_ID

Once ``list --object PORT_ID`` shows the row ``completed``, or no longer
shows it, the controller has removed the GUID. If the row fails again, look
at ``neutron-periodic-workers``' log for the controller's answer.

``retry`` only sends port deletions, and refuses one that could undo
something newer: if the journal has a newer row of the port, if the port is
bound again, or if another port with the same GUID is bound on the same
network. Delete such a row, or one that the controller can't complete, only
after checking on the controller that the port's GUID is in no partition key
that it shouldn't be in::

    neutron-mlnx-manage --config-file /etc/neutron/neutron.conf \
        journal delete ROW_ID

``delete`` takes several row IDs. It lists the rows, with each port's GUID
and network, and asks once before deleting them. It reports and skips any ID
whose row doesn't exist or isn't failed, for example because it has been sent
again since you listed it, deletes the others and exits with status 1. For
example, once you have checked every failed row on the controller, delete
them all::

    CONF="--config-file /etc/neutron/neutron.conf"
    IDS=$(neutron-mlnx-manage $CONF journal list --state failed -f value -c ID)
    [ -z "$IDS" ] || neutron-mlnx-manage $CONF journal delete $IDS

Either way, the ports with the same GUID that were set to ``ERROR`` stay so:
unbind and rebind them.
