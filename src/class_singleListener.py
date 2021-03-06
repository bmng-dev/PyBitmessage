import errno
import logging
import Queue
import re
import socket

import knownnodes
import protocol
import shared
import state
from bmconfigparser import BMConfigParser
from class_receiveDataThread import receiveDataThread
from class_sendDataThread import sendDataThread
from helper_threading import StoppableThread


logger = logging.getLogger(__name__)


# Only one singleListener thread will ever exist. It creates the
# receiveDataThread and sendDataThread for each incoming connection. Note
# that it cannot set the stream number because it is not known yet- the
# other node will have to tell us its stream number in a version message.
# If we don't care about their stream, we will close the connection
# (within the recversion function of the recieveData thread)


class singleListener(StoppableThread):
    def _createListenSocket(self, family):
        HOST = ''  # Symbolic name meaning all available interfaces
        # If not sockslisten, but onionhostname defined, only listen on localhost
        if not BMConfigParser().safeGetBoolean('bitmessagesettings', 'sockslisten') and ".onion" in BMConfigParser().get('bitmessagesettings', 'onionhostname'):
            if family == socket.AF_INET6 and "." in BMConfigParser().get('bitmessagesettings', 'onionbindip'):
                raise socket.error(errno.EINVAL, "Invalid mix of IPv4 and IPv6")
            elif family == socket.AF_INET and ":" in BMConfigParser().get('bitmessagesettings', 'onionbindip'):
                raise socket.error(errno.EINVAL, "Invalid mix of IPv4 and IPv6")
            HOST = BMConfigParser().get('bitmessagesettings', 'onionbindip')
        PORT = BMConfigParser().getint('bitmessagesettings', 'port')
        sock = socket.socket(family, socket.SOCK_STREAM)
        if family == socket.AF_INET6:
            # Make sure we can accept both IPv4 and IPv6 connections.
            # This is the default on everything apart from Windows
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # This option apparently avoids the TIME_WAIT state so that we can
        # rebind faster
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, PORT))
        sock.listen(2)
        return sock
        
    def stopThread(self):
        super(singleListener, self).stopThread()
        sock = getattr(self, 'socket', None)
        if not sock:
            return
        try:
            sock.shutdown(socket.SHUT_RD)
        finally:
            sock.close()

    def run(self):
        # If there is a trusted peer then we don't want to accept
        # incoming connections so we'll just abandon the thread
        if state.trustedPeer:
            return

        while BMConfigParser().safeGetBoolean('bitmessagesettings', 'dontconnect') and not self.stop_requested:
            self.wait(1)
        knownnodes.dns()
        # We typically don't want to accept incoming connections if the user is using a
        # SOCKS proxy, unless they have configured otherwise. If they eventually select
        # proxy 'none' or configure SOCKS listening then this will start listening for
        # connections. But if on SOCKS and have an onionhostname, listen
        # (socket is then only opened for localhost)
        while BMConfigParser().get('bitmessagesettings', 'socksproxytype')[0:5] == 'SOCKS' and \
            (not BMConfigParser().getboolean('bitmessagesettings', 'sockslisten') and \
            ".onion" not in BMConfigParser().get('bitmessagesettings', 'onionhostname')) and \
            not self.stop_requested:
            self.wait(5)

        logger.info('Listening for incoming connections.')

        # First try listening on an IPv6 socket. This should also be
        # able to accept connections on IPv4. If that's not available
        # we'll fall back to IPv4-only.
        try:
            sock = self._createListenSocket(socket.AF_INET6)
        except socket.error as e:
            if (isinstance(e.args, tuple) and
                e.args[0] in (errno.EAFNOSUPPORT,
                              errno.EPFNOSUPPORT,
                              errno.EADDRNOTAVAIL,
                              errno.ENOPROTOOPT,
                              errno.EINVAL)):
                sock = self._createListenSocket(socket.AF_INET)
            else:
                raise

        self.socket = sock

        # regexp to match an IPv4-mapped IPv6 address
        mappedAddressRegexp = re.compile(r'^::ffff:([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)$')

        while not self.stop_requested:
            # We typically don't want to accept incoming connections if the user is using a
            # SOCKS proxy, unless they have configured otherwise. If they eventually select
            # proxy 'none' or configure SOCKS listening then this will start listening for
            # connections.
            while BMConfigParser().get('bitmessagesettings', 'socksproxytype')[0:5] == 'SOCKS' and not BMConfigParser().getboolean('bitmessagesettings', 'sockslisten') and ".onion" not in BMConfigParser().get('bitmessagesettings', 'onionhostname') and not self.stop_requested:
                self.wait(10)
            while len(shared.connectedHostsList) > \
                BMConfigParser().safeGetInt("bitmessagesettings", "maxtotalconnections", 200) + \
                BMConfigParser().safeGetInt("bitmessagesettings", "maxbootstrapconnections", 20) \
                and not self.stop_requested:
                logger.info('We are connected to too many people. Not accepting further incoming connections for ten seconds.')

                self.wait(10)

            while not self.stop_requested:
                socketObject = None
                try:
                    socketObject, sockaddr = sock.accept()
                except socket.error as e:
                    if e.errno == errno.EINVAL:
                        break
                    if isinstance(e.args, tuple) and \
                        e.args[0] in (errno.EINTR,):
                        continue
                    self.wait(1)
                    continue

                (HOST, PORT) = sockaddr[0:2]

                # If the address is an IPv4-mapped IPv6 address then
                # convert it to just the IPv4 representation
                md = mappedAddressRegexp.match(HOST)
                if md != None:
                    HOST = md.group(1)

                # The following code will, unfortunately, block an
                # incoming connection if someone else on the same LAN
                # is already connected because the two computers will
                # share the same external IP. This is here to prevent
                # connection flooding.
                # permit repeated connections from Tor
                if HOST in shared.connectedHostsList and \
                    (".onion" not in BMConfigParser().get('bitmessagesettings', 'onionhostname') or not protocol.checkSocksIP(HOST)):
                    socketObject.close()
                    logger.info('We are already connected to ' + str(HOST) + '. Ignoring connection.')
                else:
                    break

            if self.stop_requested:
                if socketObject:
                    try:
                        socketObject.shutdown(socket.SHUT_RDWR)
                    except:
                        logger.exception()
                    finally:
                        socketObject.close()
                break

            sendDataThreadQueue = Queue.Queue() # Used to submit information to the send data thread for this connection.
            socketObject.settimeout(20)

            sd = sendDataThread(sendDataThreadQueue, socketObject, HOST, PORT, -1)
            sd.start()

            rd = receiveDataThread(socketObject, HOST, PORT, -1, sendDataThreadQueue, sd.objectHashHolderInstance)
            rd.daemon = True  # close the main program even if there are threads left
            rd.start()

            logger.info('connected to ' + HOST + ' during INCOMING request.')

