import time
import socket
import struct
import errno
import random
from select import select
from django.conf import settings
from graphite.render.hashing import ConsistentHashRing
from graphite.logger import log

try:
  import cPickle as pickle
except ImportError:
  import pickle


class CarbonLinkPool:
  def __init__(self, hosts, timeout):
    self.hosts = [ (server, instance) for (server, port, instance) in hosts ]
    self.ports = dict( ((server, instance), port) for (server, port, instance) in hosts )
    self.timeout = float(timeout)
    servers = set([server for (server, port, instance) in hosts])
    if len(servers) < settings.REPLICATION_FACTOR:
      raise Exception("REPLICATION_FACTOR=%d cannot exceed servers=%d" % (settings.REPLICATION_FACTOR, len(servers)))

    self.hash_ring = ConsistentHashRing(self.hosts)
    self.connections = {}
    self.last_failure = {}
    # Create a connection pool for each host
    for host in self.hosts:
      self.connections[host] = set()

  def select_host(self, metric):
    "Returns the carbon host that has data for the given metric"
    nodes = []
    servers = set()
    for node in self.hash_ring.get_nodes(metric):
      (server, instance) = node
      if server in servers:
        continue
      servers.add(server)
      nodes.append(node)
      if len(servers) >= settings.REPLICATION_FACTOR:
        break

    available = [ n for n in nodes if self.is_available(n) ]
    return random.choice(available or nodes)

  def is_available(self, host):
    now = time.time()
    last_fail = self.last_failure.get(host, 0)
    return (now - last_fail) < settings.CARBONLINK_RETRY_DELAY

  def get_connection(self, host):
    # First try to take one out of the pool for this host
    (server, instance) = host
    port = self.ports[host]
    connectionPool = self.connections[host]
    try:
      return connectionPool.pop()
    except KeyError:
      pass #nothing left in the pool, gotta make a new connection

    log.cache("CarbonLink creating a new socket for %s" % str(host))
    connection = socket.socket()
    connection.settimeout(self.timeout)
    try:
      connection.connect( (server, port) )
    except:
      self.last_failure[host] = time.time()
      raise
    else:
      connection.setsockopt( socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1 )
      return connection

  def query(self, metric_path):
    host = self.select_host(metric_path)
    conn = self.get_connection(host)
    try:
      self.send_request(conn, metric_path)
      results = self.recv_response(conn)
    except:
      self.last_failure[host] = time.time()
      raise
    else:
      log.cache("CarbonLink query for %s returned %d datapoints" % (metric_path, len(results)))
      self.connections[host].add(conn)
      return results

  def send_request(self, conn, metric_path):
    len_prefix = struct.pack("!L", len(metric_path))
    request_packet = len_prefix + metric_path
    conn.sendall(request_packet)

  def recv_response(self, conn):
    len_prefix = recv_exactly(conn, 4)
    body_size = struct.unpack("!L", len_prefix)[0]
    body = recv_exactly(conn, body_size)
    return pickle.loads(body)


# Socket helper functions
def still_connected(sock):
  is_readable = select([sock], [], [], 0)[0]
  if is_readable:
    try:
      recv_buf = sock.recv(1, socket.MSG_DONTWAIT|socket.MSG_PEEK)

    except socket.error, e:
      if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
        return True
      else:
        raise

    else:
      return bool(recv_buf)

  else:
    return True


def recv_exactly(conn, num_bytes):
  buf = ''
  while len(buf) < num_bytes:
    data = conn.recv( num_bytes - len(buf) )
    if not data:
      raise Exception("Connection lost")
    buf += data

  return buf


#parse hosts from local_settings.py
hosts = []
for host in settings.CARBONLINK_HOSTS:
  parts = host.split(':')
  server = parts[0]
  port = int( parts[1] )
  if len(parts) > 2:
    instance = parts[2]
  else:
    instance = None

  hosts.append( (server, int(port), instance) )


#A shared importable singleton
CarbonLink = CarbonLinkPool(hosts, settings.CARBONLINK_TIMEOUT)
