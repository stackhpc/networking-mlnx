#! /bin/sh

# Copyright (C) 2014 VA Linux Systems Japan K.K.
# Copyright (C) 2014 YAMAMOTO Takashi <yamamoto at valinux co jp>
# All Rights Reserved.
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

# The purpose of this script is to avoid casual introduction of more
# bash dependency.  Please consider alternatives before commiting code
# which uses bash specific features.

export TMPDIR=`/bin/mktemp -d`
trap "rm -rf $TMPDIR" EXIT

FAILURES=$TMPDIR/failures


check_opinionated_shell () {
    # Check that shell scripts are not bash opinionated (ignore comments though)
    # If you cannot avoid the use of bash, please change the EXPECTED var below.
    OBSERVED=$(grep -E '^([^#]|#!).*bash' tox.ini tools/* | wc -l)
    EXPECTED=5
    if [ ${EXPECTED} -ne ${OBSERVED} ]; then
        echo "Bash usage has been detected!" >>$FAILURES
    fi
}


check_no_symlinks_allowed () {
    # Symlinks break the package build process, so ensure that they
    # do not slip in, except hidden symlinks.
    if [ $(find . -type l ! -path '*/\.*' | wc -l) -ge 1 ]; then
        echo "Symlinks are not allowed!" >>$FAILURES
    fi
}


# Add your checks here...
check_opinionated_shell
check_no_symlinks_allowed

# Fail, if there are emitted failures
if [ -f $FAILURES ]; then
    cat $FAILURES
    exit 1
fi
