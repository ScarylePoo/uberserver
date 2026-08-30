from socketserver import UDPServer,DatagramRequestHandler
from twisted.internet import reactor
import sys, logging

class CustomUDPServer(UDPServer):
	def Bind(self, root):
		self._root = root

	def finish_request(self, request, client_address):
		if '_root' in dir(self):
			self.RequestHandlerClass(request, client_address, self, self._root)
		else:
			pass # not bound to _root yet, no point in handling UDP

class handler(DatagramRequestHandler):
	def __init__(self, request, client_address, server, root):
		self._root = root
		self.request = request
		self.client_address = client_address
		self.server = server
		try:
			self.setup()
			self.handle()
			self.finish()
		finally:
			sys.exc_traceback = None    # Help garbage collection

	def handle(self):
		addr = self.client_address
		# the datagram arrives as bytes and usernames is keyed by str, so without decoding it
		# the test below can never be true and nothing downstream of it has ever run.
		msg = self.rfile.readline().decode("utf-8", errors="replace").rstrip()
		#print "%s from %s(%d)" % (msg, addr[0], addr[1])
		self.wfile.write(b'PONG')
		# a cheap filter on this thread so stray datagrams do not queue reactor work. It can be
		# stale, so _udp_packet resolves the user again on the reactor and is the real check.
		if msg in self._root.usernames:
			# this runs on the UDP server's own thread (server.py starts it there), and
			# _udp_packet mutates client state and writes to a client's transport, neither of
			# which is safe off the reactor. Client has no _protocol attribute either, so the
			# protocol comes from the root.
			reactor.callFromThread(self._root.protocol._udp_packet, msg, addr[0], addr[1])

class NATServer:
	def __init__(self, port):
		self.s = CustomUDPServer(('',port), handler)
		logging.info("Awaiting UDP messages on port %d" % port)

	def bind(self, root):
		self.s.Bind(root)

	def start(self):
		self.s.serve_forever()

	def close(self):
		self.s.close()
