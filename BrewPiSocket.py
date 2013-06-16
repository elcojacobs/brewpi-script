__author__ = 'Elco'

import sys
import BrewPiUtil as util
import socket
import os


class BrewPiSocket:
	"""
	A wrapper class for the standard socket class.
	"""

	def __init__(self, cfg):
		""" Creates a BrewPi socket object and reads the settings from a BrewPi ConfigObj.
		Does not create a socket, just prepares the settings.

		Args:
		cfg: a ConfigObj object form a BrewPi config file
		"""

		self.type = 'f'  # default to file socket
		self.file = None
		self.host = 'localhost'
		self.port = None
		self.sock = 0

		isWindows = sys.platform.startswith('win')
		useInternetSocket = bool(cfg.get('useInternetSocket', isWindows))
		if useInternetSocket:
			self.port = cfg.get('socketPort', 6332)
			self.type = 'i'
		else:
			self.file = util.addSlash(cfg['scriptPath']) + 'BEERSOCKET'

	def __repr__(self):
		"""
		This special function ensures BrewPiSocket is printed as a dict of its member variables in print statements.
		"""
		return repr(self.__dict__)

	def create(self):
		""" Creates a socket socket based on the settings in the member variables and assigns it to self.sock
		This function deletes old sockets for file sockets, so do not use it to connect to a socket that is in use.
		"""
		if self.type == 'i':  # Internet socket
			self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			self.sock.bind((self.host, self.port))
			util.logMessage('Bound to TCP socket on port %d ' % self.port)
		else:
			if os.path.exists(self.file):
				# if socket already exists, remove it
				os.remove(self.file)
			self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			self.sock.bind(self.file)  # Bind BEERSOCKET
			# set all permissions for socket
			os.chmod(self.file, 0777)

	def listen(self):
		"""
		Start listing on the socket, with default settings for blocking/backlog/timeout
		"""
		self.sock.setblocking(1)  # set socket functions to be blocking
		self.sock.listen(10)  # Create a backlog queue for up to 10 connections
		self.sock.settimeout(0.1)  # set to block 0.1 seconds, for instance for reading from the socket

	def read(self):
		"""
		Accept a connection from the socket and reads the incoming message.

		Returns:
		conn: socket object when an incoming connection is accepted, otherwise returns False
		msgType: the type of the message received on the socket
		msg: the message body
		"""
		conn = False
		msgType = ""
		msg = ""
		try:
			conn, addr = self.sock.accept()
			message = conn.recv(4096)
			if "=" in message:
				msgType, msg = message.split("=", 1)
			else:
				msgType = message
		except socket.timeout:
			conn = False
		finally:
			return conn, msgType, msg

