from twisted.internet.protocol import Factory
from twisted.internet import protocol
from twisted.internet import reactor
from twisted.protocols.policies import TimeoutMixin
import Client
import traceback
import logging
import resource

maxhandles, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
maxclients = int(maxhandles / 2)

class Chat(protocol.Protocol, Client.Client, TimeoutMixin):

	def __init__(self, root):
		self.root = root
		self.TLS = False
		# 2.3: pending reactor call that disconnects this client if its write
		# buffer stays backed up past the grace period (None when not backed up).
		self._write_overflow_call = None
		assert(self.root.userdb != None)

	def connectionMade(self):
		try:
			clientcount = len(self.root.clients)
			if clientcount >= maxclients:
				logging.error("to many connections: %d > %d" %(clientcount, maxclients))
				self.transport.write(b"DENIED to many connections, sorry!\n")
				self.transport.abortConnection()
				return

			self.root.session_id += 1
			self.session_id = self.root.session_id
			assert(self.session_id not in self.root.clients)
			self.root.clients[self.session_id] = self
			self.setTimeout(60)
			peer = (self.transport.getPeer().host, self.transport.getPeer().port)
			Client.Client.__init__(self, self.root, peer, self.session_id)
			# 2.3: bound the per-client send buffer. Raise the high-water mark above
			# Twisted's 64KB default so a large login state-dump to a healthy client
			# doesn't needlessly trip it, then register as a streaming producer so
			# pauseProducing/resumeProducing tell us when a client stops reading.
			self.transport.bufferSize = self.root.write_buffer_highwater
			self.transport.registerProducer(self, True)
			self.root.protocol._new(self)
		except Exception as e:
			logging.error("Error in adding client: %s %s %s" %(str(e), self.transport.getPeer().host, str(traceback.format_exc())))
			self.transport.abortConnection()

	def connectionLost(self, reason):
		if not hasattr(self, 'session_id'): # this func is called after a client has dc'ed
			return
		self._cancelWriteOverflow()
		self.root.protocol._remove(self, str(reason.value))
		del self.root.clients[self.session_id]

	##
	## 2.3: write-buffer backpressure (IPushProducer)
	##
	## We are registered as a streaming producer on our own transport, so Twisted
	## calls pauseProducing() when the send buffer exceeds write_buffer_highwater
	## and resumeProducing() once it has fully drained. We can't actually pause the
	## source (broadcasts are pushed from other clients), so instead we treat a
	## sustained backlog as a stalled/slow reader and disconnect it after a grace
	## period. A momentary backlog (e.g. a big login dump in flight) drains and
	## cancels the timer, so healthy clients are never dropped.

	def pauseProducing(self):
		if self._write_overflow_call is None:
			self._write_overflow_call = reactor.callLater(self.root.write_buffer_grace, self._writeBufferOverflow)

	def resumeProducing(self):
		self._cancelWriteOverflow()

	def stopProducing(self):
		self._cancelWriteOverflow()

	def _cancelWriteOverflow(self):
		if self._write_overflow_call is not None:
			if self._write_overflow_call.active():
				self._write_overflow_call.cancel()
			self._write_overflow_call = None

	def _writeBufferOverflow(self):
		self._write_overflow_call = None
		logging.info("disconnecting slow/stalled reader %s (%s): write buffer over %i bytes for %is" % (
			getattr(self, 'username', '') or '?', self.ip_address, self.root.write_buffer_highwater, self.root.write_buffer_grace))
		self.transport.abortConnection()

	def removePWs(self, data):
		# remove pw from LOGIN msg, to avoid appearing in logfile
		data = data.decode("UTF-8")
		if not "LOGIN" in data: return data.encode('UTF-8')
		words = data.split(" ")
		if data.startswith('#') and len(words)>= 3:
			words[3] = "*"
		elif data.startswith('LOGIN') and len(words)>= 2:
			words[2] = "*"
		data = " ".join(words)
		return data.encode('UTF-8')
		
	def dataReceived(self, data):
		try:
			if self.username:
				self.resetTimeout() #reset timeout for authentificated users when data is received
			self.Handle(data.decode("utf-8"))
			self._root.session_manager.commit_guard()			
		except UnicodeDecodeError:
			self.Remove("Invalid utf-8 data received, closing connection")
			self._root.session_manager.rollback_guard()
		except Exception as e:
			data = self.removePWs(data)
			logging.error("Error in handling data from client: %s %s:%s \nexception: %s\ncommand:  %s\n%s" % (self.username, self.ip_address, self.port, str(e), data, str(traceback.format_exc())))
			self._root.session_manager.rollback_guard()
		finally:
			self._root.session_manager.close_guard()
			
	def timeoutConnection(self):
		self.transport.abortConnection()

	def Remove(self, reason='Quit'):
		self.transport.abortConnection()

	def StartTLS(self):
		try:
			self.transport.startTLS(self.root.cert)
			self.TLS = True
		except Exception as e:
			logging.error("Error in handling data from client: %s, %s" % (str(e), str(traceback.format_exc())))
			self.transport.abortConnection()

class ChatFactory(Factory):

	def __init__(self, root):
		self.root = root # maps user names to Chat instances
		assert(self.root.userdb != None)

	def buildProtocol(self, addr):
		return Chat(self.root)

