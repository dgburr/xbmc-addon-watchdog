'''
    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
import os
import re
import traceback
import simplejson
import threading
import pykka
import watchdog
import xbmc
import xbmcaddon
import xbmcvfs
from time import sleep
from urllib import unquote
from functools import partial
from watchdog.events import FileSystemEventHandler

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
CLEAN = ADDON.getSetting('clean') == 'true'
POLLING = int(ADDON.getSetting('method'))
POLLING_METHOD = int(ADDON.getSetting('pollingmethod'))
RECURSIVE = not (ADDON.getSetting('nonrecursive') == 'true') or not POLLING
WATCH_VIDEO = ADDON.getSetting('watchvideo') == 'true'
WATCH_MUSIC = ADDON.getSetting('watchmusic') == 'true'
DELAY = int("0"+ADDON.getSetting('delay')) or 1
POLLING_INTERVAL = int("0"+ADDON.getSetting('pollinginterval')) or 4
SHOW_NOTIFICATIONS = ADDON.getSetting('notifications') == 'true'
PAUSE_ON_PLAYBACK = ADDON.getSetting('pauseonplayback') == 'true'
FORCE_GLOBAL_SCAN = ADDON.getSetting('forceglobalscan') == 'true'
EXTENSIONS = "|.nsv|.m4a|.flac|.aac|.strm|.pls|.rm|.rma|.mpa|.wav|.wma|.ogg|.mp3|.mp2|.m3u|.mod|.amf|.669|.dmf|.dsm|.far|.gdm|.imf|.it|.m15|.med|.okt|.s3m|.stm|.sfx|.ult|.uni|.xm|.sid|.ac3|.dts|.cue|.aif|.aiff|.wpl|.ape|.mac|.mpc|.mp+|.mpp|.shn|.zip|.rar|.wv|.nsf|.spc|.gym|.adx|.dsp|.adp|.ymf|.ast|.afc|.hps|.xsp|.xwav|.waa|.wvs|.wam|.gcm|.idsp|.mpdsp|.mss|.spt|.rsd|.mid|.kar|.sap|.cmc|.cmr|.dmc|.mpt|.mpd|.rmt|.tmc|.tm8|.tm2|.oga|.url|.pxml|.tta|.rss|.cm3|.cms|.dlt|.brstm|.wtv|.mka|.m4v|.3g2|.3gp|.nsv|.tp|.ts|.ty|.strm|.pls|.rm|.rmvb|.m3u|.ifo|.mov|.qt|.divx|.xvid|.bivx|.vob|.nrg|.img|.iso|.pva|.wmv|.asf|.asx|.ogm|.m2v|.avi|.bin|.dat|.mpg|.mpeg|.mp4|.mkv|.avc|.vp3|.svq3|.nuv|.viv|.dv|.fli|.flv|.rar|.001|.wpl|.zip|.vdr|.dvr-ms|.xsp|.mts|.m2t|.m2ts|.evo|.ogv|.sdp|.avs|.rec|.url|.pxml|.vc1|.h264|.rcv|.rss|.mpls|.webm|.bdmv|.wtv|.m4v|.3g2|.3gp|.nsv|.tp|.ts|.ty|.strm|.pls|.rm|.rmvb|.m3u|.m3u8|.ifo|.mov|.qt|.divx|.xvid|.bivx|.vob|.nrg|.img|.iso|.pva|.wmv|.asf|.asx|.ogm|.m2v|.avi|.bin|.dat|.mpg|.mpeg|.mp4|.mkv|.avc|.vp3|.svq3|.nuv|.viv|.dv|.fli|.flv|.rar|.001|.wpl|.zip|.vdr|.dvr-ms|.xsp|.mts|.m2t|.m2ts|.evo|.ogv|.sdp|.avs|.rec|.url|.pxml|.vc1|.h264|.rcv|.rss|.mpls|.webm|.bdmv|.wtv|"

class XBMCActor(pykka.ThreadingActor):
  """ Messaging interface to xbmc's executebuiltin calls """
  def _xbmc_is_busy(self):
    sleep(1) # visibility cant be immediately trusted. Give xbmc time to render
    return ((xbmc.Player().isPlaying() and PAUSE_ON_PLAYBACK)
        or xbmc.getCondVisibility('Library.IsScanning')
        or xbmc.getCondVisibility('Window.IsActive(10101)'))
  
  def scan(self, library, path):
    """ Tell xbmc to scan. Returns immediately when scanning has started. """
    while self._xbmc_is_busy():
      pass
    log("scanning %s (%s)" % (path, library))
    if library == 'video' and FORCE_GLOBAL_SCAN:
      xbmc.executebuiltin("UpdateLibrary(video)")
    else:
      xbmc.executebuiltin("UpdateLibrary(%s,%s)" % (library, escape_param(path)))
  
  def clean(self, library, path=None):
    """ Tell xbmc to clean. Returns immediately when scanning has started. """
    while self._xbmc_is_busy():
      pass
    log("cleaning %s library" % library)
    xbmc.executebuiltin("CleanLibrary(%s)" % library)


class EventQueue(pykka.ThreadingActor):
  """ Handles all raw incomming events for single root path and library. """
  def __init__(self, library, path, xbmc_actor):
    super(EventQueue, self).__init__()
    def ask(msg):
      if msg == 'scan':
        return xbmc_actor.scan(library, path).get()
      elif msg == 'clean':
        return xbmc_actor.clean(library, path).get()
    self.new_worker = partial(EventQueue.Worker, ask)
    self.worker = None
  
  def _notify_worker(self, attr):
    if not(self.worker) or not(self.worker.isAlive()):
      self.worker = self.new_worker()
      setattr(self.worker, attr, True)
      self.worker.start()
    else:
      setattr(self.worker, attr, True)
  
  def scan(self):
    self._notify_worker('scan')
  
  def clean(self):
    self._notify_worker('clean')
  
  class Worker(threading.Thread):
    """
    To be able to safely skip incomming duplicate events, we need a
    normal thread that sends a single message and waits for reply.
    """
    def __init__(self, ask):
      super(EventQueue.Worker, self).__init__()
      self.ask = ask
      self.scan = False
      self.clean = False
    
    def run(self):
      sleep(DELAY)
      while True:
        if self.clean:
          self.ask('clean') # returns when scanning has started
          self.clean = False
        if self.scan:
          self.ask('scan')
          self.scan = False
        if not(self.scan) and not(self.clean):
          return


class EventHandler(FileSystemEventHandler):
  def __init__(self, event_queue):
    super(EventHandler, self).__init__()
    self.event_queue = event_queue
  
  def on_created(self, event):
    if not self._can_skip(event, event.src_path):
      self.event_queue.scan()
  
  def on_deleted(self, event):
    if CLEAN and not self._can_skip(event, event.src_path):
      self.event_queue.clean()
  
  def on_moved(self, event):
    if CLEAN and not self._can_skip(event, event.src_path):
      self.event_queue.clean()
    if not self._can_skip(event, event.dest_path):
      self.event_queue.scan()
  
  def on_any_event(self, event):
    log("<%s> <%s>" % (event.event_type, event.src_path))
  
  def _can_skip(self, event, path):
    if not event.is_directory and path:
      _, ext = os.path.splitext(path)
      if EXTENSIONS.find('|%s|' % ext) == -1:
        log("skipping <%s> <%s>" % (event.event_type, path))
        return True
    return False


def get_media_sources(type):
  query = '{"jsonrpc": "2.0", "method": "Files.GetSources", "params": {"media": "%s"}, "id": 1}' % type
  result = xbmc.executeJSONRPC(query)
  json = simplejson.loads(result)
  ret = []
  if json.has_key('result'):
    if json['result'].has_key('sources'):
      paths = [ e['file'] for e in json['result']['sources'] ]
      for path in paths:
        #split and decode multipaths
        if path.startswith("multipath://"):
          for e in path.split("multipath://")[1].split('/'):
            if e != "":
              ret.append(unquote(e).encode('utf-8'))
        else:
          ret.append(path.encode('utf-8'))
  return ret

def escape_param(s):
  escaped = s.replace('\\', '\\\\').replace('"', '\\"')
  return '"' + escaped + '"'

def log(msg):
  xbmc.log("%s: %s" % (ADDON_ID, msg), xbmc.LOGDEBUG)

def notify(msg1, msg2):
  if SHOW_NOTIFICATIONS:
    xbmc.executebuiltin("XBMC.Notification(Watchdog: %s,%s)" % (msg1, escape_param(msg2)))

def select_observer(path):
  import observers
  if os.path.exists(path): #path from xbmc appears to always be utf-8 so if it contains non-ascii and os is not utf-8, this will fail
    if POLLING:
      return observers.local_full()
    return observers.auto()
  elif re.match("^[A-Za-z]+://", path):
    if xbmcvfs.exists(path):
      observer_cls = [observers.xbmc_depth_1, observers.xbmc_depth_2, observers.xbmc_full][POLLING_METHOD]
      return observer_cls(POLLING_INTERVAL)
  return None

def watch(library, xbmc_actor):
  threads = []
  sources = get_media_sources(library)
  log("%s sources %s" % (library, sources))
  for path in sources:
    if xbmc.abortRequested:
      break
    observer = select_observer(path)
    if observer:
      try:
        event_queue = EventQueue.start(library, path, xbmc_actor).proxy()
        event_handler = EventHandler(event_queue)
        observer.schedule(event_handler, path=path, recursive=RECURSIVE)
        observer.start()
        threads.append(observer)
        threads.append(event_queue)
        log("watching <%s> using %s" % (path, observer))
      except Exception as e:
        traceback.print_exc()
        log("failed to watch <%s>" % path)
        notify("Failed to watch", path)
        continue
    else:
      log("not watching <%s>. does not exist" % path)
      notify("Path does not exist", path)
  return threads

def main():
  xbmc_actor = XBMCActor.start().proxy()
  threads = []
  if WATCH_VIDEO:
    threads.extend(watch('video', xbmc_actor))
  if WATCH_MUSIC:
    threads.extend(watch('music', xbmc_actor))
  while not xbmc.abortRequested:
    sleep(1)
  for th in threads:
    try:
      th.stop()
    except Exception as e:
      traceback.print_exc()
      continue
  xbmc_actor.stop()

if __name__ == "__main__":
  main()
