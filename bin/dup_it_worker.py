#!/usr/bin/python
#
# Copyright (c) 2010 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Worker process for dup-it.

Since AppEngine would not be able to handle mirroring very large files,
this worker process will do the heavy lifting under the direction of the server.
"""


import datetime
import optparse
import os
import pickle
import sys
import time
import urllib

import mirror


def GetJob(options):
  params = urllib.urlencode({
      'password': options.password,
  })
  f = urllib.urlopen(options.server + '/start', params)
  job = pickle.loads(f.read())
  f.close()
  return job


def LogStatus(options, job, fraction_complete, status, result=None):
  params = {
      'password': options.password,
      'status': status,
      'work_id': job['work_id'],
      'fraction_complete': str(fraction_complete)
  }
  if result is not None:
    params['result'] = str(result)
  params = urllib.urlencode(params)
  f = urllib.urlopen(options.server + '/status', params)
  ret = f.read()
  f.close()
  return ret


class Progress(object):
  def __init__(self, options, job):
    self.options = options
    self.job = job
    self.last_update = datetime.datetime.utcnow()
    self.last_status = 'running'

  def __call__(self, fraction):
    tm = datetime.datetime.utcnow()
    if tm - self.last_update < datetime.timedelta(seconds=1):
      return self.last_status
    ret = LogStatus(self.options, self.job,
                    status='running', fraction_complete=fraction)
    self.last_update = tm
    self.last_status = ret
    return ret


def DoJob(options, job):
  done = False
  result = 'success'
  jtype = job['type']
  if jtype == 'sleep':
    time.sleep(job['duration'])
    return False
  elif jtype == 'quit':
    done = True
  elif jtype == 'mirror':
    try:
      result = mirror.Mirror(job['source'], job['destination'],
                             Progress(options, job))
    except Exception, e:
      sys.stderr.write('ERROR - mirror failed: %s\n' % str(e))
  else:
    result = 'bad job'
  LogStatus(options, job, result=result, status='idle',
            fraction_complete=1.0)
  return done


def main(argv):
  parser = optparse.OptionParser()
  parser.add_option('-s', '--server', dest='server',
                    default='https://dup-it.appspot.com',
                    help='server to work for')
  parser.add_option('-p', '--password', dest='password',
                    default=None,
                    help='password for worker access')
  (options, args) = parser.parse_args(args=argv[1:])
  if args or not options.password:
    parser.print_help()
    return 1

  done = False
  while not done:
    try:
      job = GetJob(options)
    except (EOFError, KeyError):
      time.sleep(5)
      continue
    done = DoJob(options, job)
    # Run once.
    return 0

  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
