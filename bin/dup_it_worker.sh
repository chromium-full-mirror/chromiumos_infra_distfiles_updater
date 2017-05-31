#!/bin/bash

# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# This script is run on a GCE instance to do the heavy-lifting for the distfile
# mirroring service. The GCE instance is expected to
# - set custom metadata (via the GCE web UI) with the key
#   "dup_it_passwd" with value set to the passoword obtained from
#   https://dupit-hr.appspot.com
# - kick this script on startup and reboot on exit from this script (to start
#   all over again)

set -eu

DUP_IT_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/dup_it_url"
ATTR_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes/dup_it_passwd"
TOP_DIR=$(dirname "$(dirname "$(readlink -f "$0")")")
DUP_IT_SCRIPT=${TOP_DIR}/bin/dup_it_worker.py
LOG_DIR=${TOP_DIR}/logs
LOG_FILE=${LOG_DIR}/dup_it_worker.log

mkdir -p "${LOG_DIR}"

# Save the last 5 copies of ${OUTPUT}, numbered in order
# from most to least recent.
prev=.5
for suffix in .4 .3 .2 .1 ''; do
  mv -f ${LOG_FILE}${suffix} ${LOG_FILE}${prev} || :
  prev=${suffix}
done

dup_it_url=$(curl -H "Metadata-Flavor: Google" ${DUP_IT_URL})
dup_it_passwd=$(curl -H "Metadata-Flavor: Google" ${ATTR_URL})

(date && \
 git fetch -q origin && \
 git checkout -qf --detach origin/master && \
 git clean -qfd && \
 ${DUP_IT_SCRIPT} -s "${dup_it_url}" -p "${dup_it_passwd}" && \
 date) >> "${LOG_FILE}" 2>&1

# Sleep [2, 4) hours.
sleep_time=$(( ( RANDOM % 2 ) + 2 )).$(( ( RANDOM % 60 ) ))
echo "Sleeping for ${sleep_time} hours" >> ${LOG_FILE}
sleep ${sleep_time}h
