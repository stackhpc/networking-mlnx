=========
Releasing
=========

networking-mlnx is not part of the OpenStack release process, so its
releases are made by hand by the members of the ``networking-mlnx-release``
Gerrit group. Only they can create branches and push tags.

Version numbers
===============

Each release series has the same major version as Neutron's release in that
series:

======  =============  ===============
Series  Major version  Branch
======  =============  ===============
2025.1  26             stable/2025.1
2025.2  27             stable/2025.2
2026.1  28             stable/2026.1
2026.2  29             stable/2026.2
2027.1  30             master
======  =============  ===============

pbr reads the version from the latest tag that is an ancestor of the commit
being built, and reno uses the same tags to group the release notes. Tags
that are only on stable branches are invisible on master, so the release
candidate of each series is tagged on master and the stable branch is created
from it, as in Neutron. Otherwise master builds with an older version than
the stable branches, and its release notes are listed under an old version.

After the stable branch is created, master builds as a development version
after the release candidate (for example ``30.0.0.0rc2.devN``), or after the
release (``30.0.1.devN``) if the release is tagged on the release candidate's
commit. Neutron's master does the same.

Master also has a ``30.0.0.0b1`` tag. It was a one-off, to give master a
version above the stable branches before this process was followed, and
``30.0.0.0rc1`` supersedes it.

Before the first release
========================

Zuul uploads releases to PyPI as the ``openstackci`` account, so an owner of
the `networking-mlnx PyPI project <https://pypi.org/project/networking-mlnx/>`_
must add ``openstackci`` as an owner first. Until then every release job fails,
and neither the sdist nor the wheel is published. See `Publishing`_.

Signing tags
============

Gerrit only accepts signed tags. It checks that a tag has a signature, not
who made it, and accepts both GPG and SSH signatures. To sign with an SSH key,
give git its public key (git 2.34 or later)::

    git -c gpg.format=ssh -c user.signingkey=~/.ssh/id_ed25519.pub \
        tag -s <tag> -m "Create tag <tag> <branch>" <commit>

With a forwarded agent and no key file, use
``user.signingkey="key::$(ssh-add -L | grep -m1 <key comment>)"`` instead.
The ``<branch>`` in the message is only for information; the existing tags
use it.

Check the tag before pushing it, because only Gerrit administrators can
delete a pushed tag. Verifying an SSH signature needs an allowed signers
file, which must be a regular file::

    echo "* $(cat ~/.ssh/id_ed25519.pub)" > /tmp/allowed_signers
    git -c gpg.ssh.allowedSignersFile=/tmp/allowed_signers tag -v <tag>
    git fetch gerrit
    git branch -r --contains <tag>
    git push gerrit <tag>

Cutting a release
=================

Do these steps when Neutron releases a new series, using 2027.1 and version
30 as the example.

#. **Tag the release candidate on master.** Tag the master commit you want
   to release as ``30.0.0.0rc1``. Because this tag is on master, master and
   the new stable branch both contain it.

#. **Create the stable branch from the tag.** In Gerrit, open the
   ``x/networking-mlnx`` repository's branches page and create
   ``stable/2027.1`` from the commit that ``30.0.0.0rc1`` points to (``git
   rev-parse 30.0.0.0rc1^{commit}``), or run::

       ssh -p 29418 <user>@review.opendev.org \
           gerrit create-branch x/networking-mlnx stable/2027.1 \
           $(git rev-parse 30.0.0.0rc1^{commit})

#. **Point the stable branch at its series.** Propose a change to
   ``stable/2027.1`` titled ``[stable-only] Update branch for 2027.1
   release`` that:

   * adds ``defaultbranch=stable/2027.1`` to ``.gitreview``,
   * changes the ``constraints`` URL in ``tox.ini`` from
     ``.../constraints/upper/master`` to ``.../constraints/upper/2027.1``,
     and
   * adds an upper bound of the next major version to the ``neutron`` extra
     in ``pyproject.toml``, for example ``<31.0.0``. Neutron is not in
     upper-constraints, so without it ``tox`` installs the newest Neutron
     release. Keep the lower bound low enough to accept the Neutron version
     that exists at the time, because pip resolves the extra from PyPI
     before Zuul's unit test jobs replace it with Neutron's branch of the
     same name. If the release is tagged on the release candidate's commit,
     the bound is first published in the next point release.

#. **Add the series to the release notes on master.** Propose a change to
   master that adds ``releasenotes/source/2027.1.rst``::

       ===========================
       2027.1 Series Release Notes
       ===========================

       .. release-notes::
          :branch: stable/2027.1

   lists ``2027.1`` in ``releasenotes/source/index.rst`` after
   ``unreleased``, and adds the next series to the table in
   `Version numbers`_, replacing ``master`` with ``stable/2027.1``.

#. **Tag the release.** When the branch change has merged, tag ``30.0.0``:

   * If nothing has been fixed since the release candidate, tag the same
     commit as ``30.0.0.0rc1``, as Nova and Keystone do. That commit is on
     master, so master then builds as a version after ``30.0.0``.
   * Otherwise, backport the fixes and tag the tip of ``stable/2027.1``.

Bug fix releases
================

Tag later releases on the stable branch, following semantic versioning:
increase the last number for bug fixes (``30.0.1``), and the middle number if
a backport adds a feature or configuration option (``30.1.0``). Stable branch
policy rarely allows such backports. Add a release note to every backported
change that affects users.

Publishing
==========

Pushing a tag starts Zuul jobs for the ``x/networking-mlnx`` project:

* Release tags such as ``30.0.0`` run in the ``release`` pipeline, and
  release candidate tags such as ``30.0.0.0rc1`` in the ``pre-release``
  pipeline. Their ``release-openstack-python`` job builds the sdist and
  wheel, uploads them to PyPI, and then publishes them on
  `tarballs.opendev.org <https://tarballs.opendev.org/x/networking-mlnx/>`_.
  If the PyPI upload fails, neither is published. To try again, ask in
  ``#opendev`` on OFTC for the tag to be re-enqueued.
* The ``announce-release`` and ``propose-update-constraints`` jobs run after
  it, and do nothing because the tag messages have no ``meta:`` lines. Don't
  add any.
* The release notes are rebuilt for every tag, and for every change that
  merges on any branch. They are published at
  https://docs.openstack.org/releasenotes/networking-mlnx/.

Check the results in the `Zuul builds
<https://zuul.opendev.org/t/openstack/builds?project=x%2Fnetworking-mlnx>`_.
