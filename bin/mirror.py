#!/usr/bin/python
#
# Copyright 2010 Google Inc. All Rights Reserved.

"""Mirroring of http to Google Storage."""

import formatter
import htmllib
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib
import urlparse


NUM_THREADS = 40


def RunCommand(cmd):
  try:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (p_stdout, p_stderr) = p.communicate()
    return (p.returncode, p_stdout, p_stderr)
  except OSError:
    return (-1234, '', '')
  except ValueError:
    return (-2345, '', '')


class GatherLinks(htmllib.HTMLParser):
  def __init__(self):
    self.links = []
    f = formatter.NullFormatter()
    htmllib.HTMLParser.__init__(self, f, 0)

  def anchor_bgn(self, href, name, type):
    self.links.append(href)


def CrawlTree(url, page_limit):
  """Gather all the non-html pages rooted at an url.

  Args:
    url: Root url to crawl from.
    page_limit: Maximum number of pages to download.
  Returns:
    A list of full urls of the leaf items found.
  """
  pending = [url]
  visited = set()
  objs = set()
  while pending:
    # Get an unvisited page.
    page = pending.pop(0)
    if not page.startswith(url):
      continue
    if page in visited or page in objs:
      continue
    if len(visited) >= page_limit:
      break
    # Accumulate objects.
    if not (page.endswith('.html') or
            page.endswith('.htm') or
            page.endswith('/')):
      objs.add(page)
      continue
    # Add it to visited.
    visited.add(page)
    # Download it.
    pg = urllib.urlopen(page)
    content = pg.read()
    pg.close()
    # Find all links
    parser = GatherLinks()
    parser.feed(content)
    parser.close()
    # Expand links.
    links = [urlparse.urljoin(page, lnk) for lnk in parser.links]
    links = [lnk.split('#')[0] for lnk in links]
    links = [lnk.split('?')[0] for lnk in links]
    pending.extend(links)
  return sorted(list(objs))


def StateStamp(info):
  """Compute a summary for a given url's info."""
  return (
      'Last-Modified: %(last_mod)s\n'
      'ETag: %(etag)s\n'
      'Content-Length: %(length)s\n'
  ) % {
      'last_mod': ' '.join(info.getheaders('Last-Modified')),
      'etag': ' '.join(info.getheaders('ETag')),
      'length': ' '.join(info.getheaders('Content-Length')),
  }


def Worker(objects, progress, mutex, original_count,
           gsutil, source, destination):
  """Worker thread for mirroring.

  Args:
    objects: List of objects that need to be mirrored.
    progress: Callable to pass a progress fraction to.
    mutex: Mutex guarding the list of objects.
    original_count: Initial size of objects.
    gsutil: Path to gsutil.
    source: Mirroring source (http://...).
    destination: Mirroring destination (gs://...).
  """
  while True:
    # Pluck out one to work on, or quit if no more work left.
    mutex.acquire()
    if not objects:
      mutex.release()
      return
    o = objects.pop(0)
    index = original_count - len(objects)
    status = progress(float(index) / float(original_count))
    if status == 'idle':
      print 'MANUALLY STOPPED !!!!!!!!!!!!!!!!!'
      while objects:
        objects.pop(0)
      mutex.release()
      return
    mutex.release()
    # Decide some paths.
    path = o[len(source):]
    gs_stamp = destination + '.dup-it/mirror-state/' + path
    dst = destination + path
    # Print the name.
    print '%d/%d %0.2f%% - %s' % (
        index, original_count,
        float(index) * 100 / float(original_count), path)
    # Get state stamp.
    fh = urllib.urlopen(o)
    state_stamp = StateStamp(fh.info())
    fh.close()
    # Get current state stamp.
    cmd = [gsutil, 'cat', gs_stamp]
    (_, current_stamp, _) = RunCommand(cmd)
    # Skip if already up to date.
    if state_stamp == current_stamp:
      continue
    # Download it.
    (f, tmp_name) = tempfile.mkstemp(suffix=os.path.splitext(o)[1])
    os.close(f)
    urllib.urlretrieve(o, tmp_name)
    # Upload it.
    cmd = [gsutil, 'cp', '-a', 'public-read', tmp_name, dst]
    (ret, _, _) = RunCommand(cmd)
    if ret != 0:
      sys.stderr.write('ERROR - failed to upload ' + path + '\n')
      continue
    # Delete it.
    os.remove(tmp_name)
    # Setup stamp.
    (f, tmp_name) = tempfile.mkstemp(suffix=os.path.splitext(o)[1])
    os.close(f)
    try:
      f = open(tmp_name, 'wb')
      f.write(state_stamp)
    finally:
      f.close()
    cmd = [gsutil, 'cp', '-a', 'public-read', tmp_name, gs_stamp]
    (ret, _, _) = RunCommand(cmd)
    if ret != 0:
      sys.stderr.write('ERROR - failed to store ' + gs_stamp + '\n')
      continue
    f.close()
    os.remove(tmp_name)


def Mirror(source, destination, progress, page_limit=200):
  """Mirror an http site to Google Storage.

  Args:
    source: Source url (http://...).
    destination: Destination url (gs://...).
    progress: A callable to call with fraction of completetion updates.
    page_limit: Maximum pages to crawl in gathering the index.
  Returns:
    0 for success, arbitrary return code otherwise.
  """
  # Pick gsutil.
  gsutil = os.environ.get('GSUTIL', 'gsutil')
  # Figure out bucket.
  m = re.match('^gs://([^/]*)(.*)$', destination)
  assert m and m.group(1)
  bucket = m.group(1)
  # Create bucket.
  cmd = [gsutil, 'mb', 'gs://' + bucket]
  # Intentionally ignoring failures.
  RunCommand(cmd)
  # Set bucket public.
  gs_path = 'gs://' + bucket
  cmd = [gsutil, 'setacl', 'public-read', gs_path]
  (ret, _, _) = RunCommand(cmd)
  if ret != 0:
    sys.stderr.write('ERROR - setacl failed on ' + gs_path + '\n')
    return 2
  # Gather list of objects.
  objects = CrawlTree(source, page_limit)
  # Spawn workers
  mutex = threading.Lock()
  workers = [threading.Thread(target=Worker,
                              args=(objects, progress, mutex,
                                    len(objects), gsutil,
                                    source, destination))
             for _ in range(0, NUM_THREADS)]
  # Start threads.
  for w in workers:
    w.start()
  # Wait for them to finish.
  for w in workers:
    w.join()
  return 0
