# Copyright (c) 2010 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import copy
import datetime
import logging
import os
import pickle
import sys
import uuid

import webapp2
from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext.webapp import template


TIMEOUT_DURATION = 10


class WorkerMetrics(db.Model):
  last_worker_connect = db.DateTimeProperty()


class WorkerPassword(db.Model):
  password = db.StringProperty()


class MirrorTask(db.Model):
  name = db.StringProperty()
  contacts = db.StringListProperty()
  source = db.StringProperty()
  destination = db.StringProperty()
  syncs_per_day = db.FloatProperty()
  last_sync = db.DateTimeProperty(auto_now_add=True)
  status = db.StringProperty()
  fraction_complete = db.FloatProperty()


class MirrorWorkLog(db.Model):
  work_id = db.StringProperty()
  start_timestamp = db.DateTimeProperty(auto_now_add=True)
  finish_timestamp = db.DateTimeProperty()
  status = db.StringProperty()
  result = db.StringProperty()
  basis_name = db.StringProperty()
  basis_contacts = db.StringListProperty()
  basis_source = db.StringProperty()
  basis_destination = db.StringProperty()
  basis_syncs_per_day = db.FloatProperty()
  percent_complete = db.FloatProperty()
  task = db.ReferenceProperty(MirrorTask)


class MirrorTaskChange(db.Model):
  timestamp = db.DateTimeProperty(auto_now_add=True)
  who = db.UserProperty()
  task = db.ReferenceProperty(MirrorTask)
  old_name = db.StringProperty()
  old_contacts = db.StringListProperty()
  old_source = db.StringProperty()
  old_destination = db.StringProperty()
  old_syncs_per_day = db.FloatProperty()
  new_name = db.StringProperty()
  new_contacts = db.StringListProperty()
  new_source = db.StringProperty()
  new_destination = db.StringProperty()
  new_syncs_per_day = db.FloatProperty()


def GetWorkerPassword():
  passwd = memcache.get('worker_password')
  if passwd is None:
    wp = WorkerPassword.all().get()
    if wp:
      passwd = wp.password
    memcache.add('worker_password', passwd, 60)
  return passwd


def CheckWorkerPassword(request_handler):
  passwd = GetWorkerPassword()
  return request_handler.request.get('password') == passwd


def CheckUserPassword(request_handler):
  uri = request_handler.request.uri
  user = users.get_current_user()
  if not user:
    request_handler.redirect(users.create_login_url(uri))
    return False
  # Only allow @google.com folks to look.
  if not user.email().endswith('@google.com'):
    return False
  return True


def UpdateWorkerMetrics():
  wm = WorkerMetrics.all().get()
  if not wm:
    wm = WorkerMetrics()
  wm.last_worker_connect = datetime.datetime.utcnow()
  wm.put()


class StartHandler(webapp2.RequestHandler):
  """Handle requests to start work."""

  def post(self):
    self.response.headers['Content-Type'] = 'text/plain'
    if not CheckWorkerPassword(self):
      self.response.write('boo\n')
      logging.debug('Bad password!')
      return
    UpdateWorkerMetrics()
    # Find work.
    tasks = MirrorTask.all().fetch(1000) or []
    picked_task = None
    scale = 1000
    for task in tasks:
      if (datetime.datetime.utcnow() >
          task.last_sync + (datetime.timedelta(days=1) * scale /
                            int(task.syncs_per_day * scale))):
        picked_task = task
        break
    if picked_task:
      # Create a job entry.
      work_id = str(uuid.uuid1())
      mwl = MirrorWorkLog()
      mwl.basis_name = picked_task.name
      mwl.basis_contacts = picked_task.contacts
      mwl.basis_source = picked_task.source
      mwl.basis_destination = picked_task.destination
      mwl.basis_syncs_per_day = picked_task.syncs_per_day
      mwl.work_id = work_id
      mwl.fraction_complete = 0.0
      mwl.task = picked_task
      mwl.status = 'running'
      mwl.put()
      # Update task.
      picked_task.last_sync = datetime.datetime.utcnow()
      picked_task.fraction_complete = 0.0
      picked_task.status = 'running'
      picked_task.put()
      # Give client info about this job.
      self.response.out.write(pickle.dumps({
          'type': 'mirror',
          'name': picked_task.name,
          'source': picked_task.source,
          'destination': picked_task.destination,
          'work_id': work_id,
      }))
    else:
      # Sleep if there's nothing to do.
      self.response.out.write(pickle.dumps({
          'type': 'sleep',
          'duration': TIMEOUT_DURATION,
      }))


class StatusHandler(webapp2.RequestHandler):
  """Handle requests to note changes in a task's status."""

  def post(self):
    if not CheckWorkerPassword(self): return
    UpdateWorkerMetrics()
    status = self.request.get('status')
    fraction_complete = float(self.request.get('fraction_complete'))
    work_id = str(self.request.get('work_id'))
    mwl = db.GqlQuery('SELECT * FROM MirrorWorkLog '
                      'WHERE work_id = :1', work_id).get()
    if mwl:
      if mwl.task.status == 'idle':
        status = 'idle'
      if status == 'idle':
        mwl.finish_timestamp = datetime.datetime.utcnow()
      mwl.status = status
      mwl.fraction_complete = fraction_complete
      mwl.put()
      # Update root task.
      mwl.task.fraction_complete = fraction_complete
      mwl.task.status = status
      mwl.task.put()
    self.response.out.write(str(status))


class SetHandler(webapp2.RequestHandler):
  """Handle requests to change tasks."""

  def post(self):
    if not CheckUserPassword(self): return
    key = self.request.get('key')
    # Optionally stop.
    if self.request.get('stop'):
      task = MirrorTask.get(key)
      task.status = 'idle'
      task.put()
      return
    # Optionally start.
    if self.request.get('start'):
      task = MirrorTask.get(key)
      scale = 1000
      stale = task.last_sync - (datetime.timedelta(days=1) * scale /
                                int(task.syncs_per_day * scale))
      stale -= datetime.timedelta(days=1)  # Just for good measure.
      task.last_sync = stale
      task.put()
      return
    # Prepare a change.
    change = MirrorTaskChange()
    # Get the old value if any.
    if key:
      task = MirrorTask.get(key)
      # Store old state.
      change.old_name = task.name
      change.old_contacts = task.contacts
      change.old_source = task.source
      change.old_destination = task.destination
      change.old_syncs_per_day = task.syncs_per_day
      if self.request.get('delete'):
        # Store the change.
        change.who = users.get_current_user()
        change.task = task
        change.put()
        # Delete it.
        task.delete()
        return
    else:
      task = MirrorTask()
      task.fraction_complete = 1.0
      task.status = 'idle'
    # Update it.
    task.name = self.request.get('name')
    task.contacts = [i.strip() for i
                     in self.request.get('contacts').split(',')]
    task.source = self.request.get('source')
    task.destination = self.request.get('destination')
    task.syncs_per_day = float(self.request.get('syncs_per_day'))
    task.put()
    # Store the change.
    change.who = users.get_current_user()
    change.task = task
    change.new_name = task.name
    change.new_contacts = task.contacts
    change.new_source = task.source
    change.new_destination = task.destination
    change.new_syncs_per_day = task.syncs_per_day
    change.put()


class AdminHandler(webapp2.RequestHandler):
  """Handle admin only tasks."""

  def post(self):
    if not CheckUserPassword(self): return
    if not users.is_current_user_admin(): return
    # Handle setting password.
    password = self.request.get('password')
    if password:
      wp = WorkerPassword.all().get()
      if not wp:
        wp = WorkerPassword()
      wp.password = password
      wp.put()
      memcache.add('worker_password', password, 60)


class HealthHandler(webapp2.RequestHandler):
  """Handle requests for health check."""

  def get(self):
    wm = WorkerMetrics.all().get()
    if not wm:
      return
    # Convert last_worker_connect to integer seconds.
    tm = datetime.datetime.utcnow()
    td = tm - wm.last_worker_connect
    last_worker_connect = td.seconds + td.days * 24 * 3600
    # Emit as json.
    self.response.headers['Content-Type'] = 'text/plain'
    self.response.out.write(json.dumps({
        'last_worker_connect': last_worker_connect,
        }))


def DescribeChange(change):
  class Description:
    pass
  desc = Description()
  desc.timestamp = change.timestamp
  desc.who = change.who.email()
  desc.name = change.old_name
  if change.new_name is None:
    desc.changes = 'DELETED'
  else:
    changes = ''
    if change.old_name != change.new_name:
      changes += 'name: %s. ' % change.new_name
    if change.old_contacts != change.new_contacts:
      changes += 'contacts: %s. ' % ', '.join(change.new_contacts)
    if change.old_source != change.new_source:
      changes += 'source: %s.' % change.new_destination
    if change.old_destination != change.new_destination:
      changes += 'destination: %s.' % change.new_destination
    if change.old_syncs_per_day != change.new_syncs_per_day:
      changes += 'daily syncs: %s. ' % str(change.new_syncs_per_day)
    desc.changes = changes
  return desc


class ConsoleHandler(webapp2.RequestHandler):
  """View log info."""

  def get(self):
    if not CheckUserPassword(self): return
    # Get tasks.
    tasks = db.GqlQuery('SELECT * FROM MirrorTask').fetch(1000) or []
    # Embelish with fields for template.
    for task in tasks:
      task.contacts_str = ', '.join(task.contacts)
      task.key_str = str(task.key())
      task.is_idle = task.status == 'idle'
      if task.status == 'running':
        task.status_str = 'running %0.2f%%' % (task.fraction_complete * 100)
      else:
        task.status_str = task.status
    # Get Changes.
    changes = db.GqlQuery('SELECT * FROM MirrorTaskChange '
                          'ORDER BY timestamp DESC').fetch(100) or []
    changes = [DescribeChange(c) for c in changes]
    # Decide if we're an admin.
    is_admin = users.is_current_user_admin()
    # Display main console.
    template_values = {
        'email': users.get_current_user().email(),
        'logout_url': users.create_logout_url('/'),
        'tasks': tasks,
        'changes': changes,
        'is_admin': is_admin,
    }
    # Add things for admins.
    if is_admin:
      passwd = GetWorkerPassword()
      template_values['password'] = passwd
    # Render the template.
    path = os.path.join(os.path.dirname(__file__), 'templates', 'console.html')
    self.response.out.write(template.render(path, template_values))


APP = webapp2.WSGIApplication([
    ('/', ConsoleHandler),
    ('/start', StartHandler),
    ('/status', StatusHandler),
    ('/set', SetHandler),
    ('/admin', AdminHandler),
    ('/health', HealthHandler),
], debug=False)
