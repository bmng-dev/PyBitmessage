import errno
import logging
import Queue
import random
import socket
import time

import socks

import knownnodes
import protocol
import queues
import shared
import state
import tr
from bmconfigparser import BMConfigParser
from class_receiveDataThread import receiveDataThread
from class_sendDataThread import sendDataThread
from helper_threading import StoppableThread


logger = logging.getLogger(__name__)


# For each stream to which we connect, several outgoingSynSender threads
# will exist and will collectively create 8 connections with peers.

class outgoingSynSender(StoppableThread):

    def __init__(self, streamNumber):
        super(outgoingSynSender, self).__init__()
        random.seed()
        self.streamNumber = streamNumber

    def _getPeer(self):
        # If the user has specified a trusted peer then we'll only
        # ever connect to that. Otherwise we'll pick a random one from
        # the known nodes
        if state.trustedPeer:
            with knownnodes.knownNodesLock:
                peer = state.trustedPeer
                knownnodes.knownNodes[self.streamNumber][peer] = time.time()
        else:
            while not self.stop_requested:
                try:
                    with knownnodes.knownNodesLock:
                        peer, = random.sample(knownnodes.knownNodes[self.streamNumber], 1)
                        priority = (183600 - (time.time() - knownnodes.knownNodes[self.streamNumber][peer])) / 183600 # 2 days and 3 hours
                except ValueError: # no known nodes
                    self.wait(1)
                    continue
                if BMConfigParser().get('bitmessagesettings', 'socksproxytype') != 'none':
                    if peer.host.find(".onion") == -1:
                        priority /= 10 # hidden services have 10x priority over plain net
                    else:
                        # don't connect to self
                        if peer.host == BMConfigParser().get('bitmessagesettings', 'onionhostname') and peer.port == BMConfigParser().getint("bitmessagesettings", "onionport"):
                            continue
                elif peer.host.find(".onion") != -1: # onion address and so proxy
                    continue
                if priority <= 0.001: # everyone has at least this much priority
                    priority = 0.001
                if (random.random() <=  priority):
                    break
                self.wait(0.01) # prevent CPU hogging if something is broken
        try:
            return peer
        except NameError:
            return state.Peer('127.0.0.1', 8444)
        
    def stopThread(self):
        super(outgoingSynSender, self).stopThread()
        sock = getattr(self, 'sock', None)
        if not sock or isinstance(sock, socket._closedsocket):
            return
        try:
            sock.settimeout(1.0)
            try:
                sock.shutdown(socket.SHUT_RDWR)
            finally:
                sock.close()
        except socket.error as ex:
            if ex.errno not in (errno.EBADF, errno.ENOTCONN):
                logger.exception('Exception shutting down outgoingSynSender')
            return

    def run(self):
        while BMConfigParser().safeGetBoolean('bitmessagesettings', 'dontconnect') and not self.stop_requested:
            self.wait(2)
        while BMConfigParser().safeGetBoolean('bitmessagesettings', 'sendoutgoingconnections') and not self.stop_requested:
            self.name = "outgoingSynSender"
            maximumConnections = 1 if state.trustedPeer else BMConfigParser().safeGetInt('bitmessagesettings', 'maxoutboundconnections')
            while len(shared.selfInitiatedConnections.get(self.streamNumber, {})) >= maximumConnections and not self.stop_requested:
                self.wait(10)
            if self.stop_requested:
                break
            peer = self._getPeer()
            while peer in shared.alreadyAttemptedConnectionsList or peer.host in shared.connectedHostsList:
                # print 'choosing new sample'
                peer = self._getPeer()
                self.wait(1)
                if self.stop_requested:
                    break
                # Clear out the shared.alreadyAttemptedConnectionsList every half
                # hour so that this program will again attempt a connection
                # to any nodes, even ones it has already tried.
                with shared.alreadyAttemptedConnectionsListLock:
                    if (time.time() - shared.alreadyAttemptedConnectionsListResetTime) > 1800:
                        shared.alreadyAttemptedConnectionsList.clear()
                        shared.alreadyAttemptedConnectionsListResetTime = int(
                            time.time())
            shared.alreadyAttemptedConnectionsList[peer] = 0
            if self.stop_requested:
                break
            self.name = "outgoingSynSender-" + peer.host.replace(":", ".") # log parser field separator
            address_family = socket.AF_INET
            # Proxy IP is IPv6. Unlikely but possible
            if BMConfigParser().get('bitmessagesettings', 'socksproxytype') != 'none':
                if ":" in BMConfigParser().get('bitmessagesettings', 'sockshostname'):
                    address_family = socket.AF_INET6
            # No proxy, and destination is IPv6
            elif peer.host.find(':') >= 0 :
                address_family = socket.AF_INET6
            try:
                self.sock = socks.socksocket(address_family, socket.SOCK_STREAM)
            except:
                """
                The line can fail on Windows systems which aren't
                64-bit compatiable:
                      File "C:\Python27\lib\socket.py", line 187, in __init__
                        _sock = _realsocket(family, type, proto)
                      error: [Errno 10047] An address incompatible with the requested protocol was used
                      
                So let us remove the offending address from our knownNodes file.
                """
                with knownnodes.knownNodesLock:
                    try:
                        del knownnodes.knownNodes[self.streamNumber][peer]
                    except KeyError:
                        pass
                logger.debug('deleting ' + str(peer) + ' from knownnodes.knownNodes because it caused a socks.socksocket exception. We must not be 64-bit compatible.')
                continue
            # This option apparently avoids the TIME_WAIT state so that we
            # can rebind faster
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.settimeout(20)
            if BMConfigParser().get('bitmessagesettings', 'socksproxytype') == 'none' and shared.verbose >= 2:
                logger.debug('Trying an outgoing connection to ' + str(peer))

                # sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            elif BMConfigParser().get('bitmessagesettings', 'socksproxytype') == 'SOCKS4a':
                if shared.verbose >= 2:
                    logger.debug ('(Using SOCKS4a) Trying an outgoing connection to ' + str(peer))

                proxytype = socks.PROXY_TYPE_SOCKS4
                sockshostname = BMConfigParser().get(
                    'bitmessagesettings', 'sockshostname')
                socksport = BMConfigParser().getint(
                    'bitmessagesettings', 'socksport')
                rdns = True  # Do domain name lookups through the proxy; though this setting doesn't really matter since we won't be doing any domain name lookups anyway.
                if BMConfigParser().getboolean('bitmessagesettings', 'socksauthentication'):
                    socksusername = BMConfigParser().get(
                        'bitmessagesettings', 'socksusername')
                    sockspassword = BMConfigParser().get(
                        'bitmessagesettings', 'sockspassword')
                    self.sock.setproxy(
                        proxytype, sockshostname, socksport, rdns, socksusername, sockspassword)
                else:
                    self.sock.setproxy(
                        proxytype, sockshostname, socksport, rdns)
            elif BMConfigParser().get('bitmessagesettings', 'socksproxytype') == 'SOCKS5':
                if shared.verbose >= 2:
                    logger.debug ('(Using SOCKS5) Trying an outgoing connection to ' + str(peer))

                proxytype = socks.PROXY_TYPE_SOCKS5
                sockshostname = BMConfigParser().get(
                    'bitmessagesettings', 'sockshostname')
                socksport = BMConfigParser().getint(
                    'bitmessagesettings', 'socksport')
                rdns = True  # Do domain name lookups through the proxy; though this setting doesn't really matter since we won't be doing any domain name lookups anyway.
                if BMConfigParser().getboolean('bitmessagesettings', 'socksauthentication'):
                    socksusername = BMConfigParser().get(
                        'bitmessagesettings', 'socksusername')
                    sockspassword = BMConfigParser().get(
                        'bitmessagesettings', 'sockspassword')
                    self.sock.setproxy(
                        proxytype, sockshostname, socksport, rdns, socksusername, sockspassword)
                else:
                    self.sock.setproxy(
                        proxytype, sockshostname, socksport, rdns)

            try:
                self.sock.connect((peer.host, peer.port))
                if self.stop_requested:
                    self.sock.shutdown(socket.SHUT_RDWR)
                    self.sock.close()
                    return
                sendDataThreadQueue = Queue.Queue() # Used to submit information to the send data thread for this connection. 

                sd = sendDataThread(sendDataThreadQueue, self.sock, peer.host, peer.port, self.streamNumber)
                sd.start()

                rd = receiveDataThread(self.sock, peer.host, peer.port, self.streamNumber, sendDataThreadQueue, sd.objectHashHolderInstance)
                rd.daemon = True  # close the main program even if there are threads left
                rd.start()

                sd.sendVersionMessage()

                logger.debug(str(self) + ' connected to ' + str(peer) + ' during an outgoing attempt.')
            except socks.GeneralProxyError as err:
                if err[0][0] in [7, 8, 9]:
                    logger.error('Error communicating with proxy: %s', str(err))
                    queues.UISignalQueue.put((
                        'updateStatusBar',
                        tr._translate(
                            "MainWindow", "Problem communicating with proxy: %1. Please check your network settings.").arg(str(err[0][1]))
                        ))
                    self.wait(1)
                    continue
                elif shared.verbose >= 2:
                    logger.debug('Could NOT connect to ' + str(peer) + ' during outgoing attempt. ' + str(err))

                deletedPeer = None
                with knownnodes.knownNodesLock:
                    """
                    It is remotely possible that peer is no longer in knownnodes.knownNodes.
                    This could happen if two outgoingSynSender threads both try to 
                    connect to the same peer, both fail, and then both try to remove
                    it from knownnodes.knownNodes. This is unlikely because of the
                    alreadyAttemptedConnectionsList but because we clear that list once
                    every half hour, it can happen.
                    """
                    if peer in knownnodes.knownNodes[self.streamNumber]:
                        timeLastSeen = knownnodes.knownNodes[self.streamNumber][peer]
                        if (int(time.time()) - timeLastSeen) > 172800 and len(knownnodes.knownNodes[self.streamNumber]) > 1000:  # for nodes older than 48 hours old if we have more than 1000 hosts in our list, delete from the knownnodes.knownNodes data-structure.
                            del knownnodes.knownNodes[self.streamNumber][peer]
                            deletedPeer = peer
                if deletedPeer:
                    str ('deleting ' + str(peer) + ' from knownnodes.knownNodes because it is more than 48 hours old and we could not connect to it.')

            except socks.Socks5AuthError as err:
                queues.UISignalQueue.put((
                    'updateStatusBar', tr._translate(
                    "MainWindow", "SOCKS5 Authentication problem: %1. Please check your SOCKS5 settings.").arg(str(err))))
            except socks.Socks5Error as err:
                if err[0][0] in [3, 4, 5, 6]:
                    # this is a more bening "error": host unreachable, network unreachable, connection refused, TTL expired
                    logger.debug('SOCKS5 error: %s', str(err))
                else:
                    logger.error('SOCKS5 error: %s', str(err))
                if err[0][0] == 4 or err[0][0] == 2:
                    state.networkProtocolAvailability[protocol.networkType(peer.host)] = False
            except socks.Socks4Error as err:
                logger.error('Socks4Error: ' + str(err))
            except socket.error as err:
                if BMConfigParser().get('bitmessagesettings', 'socksproxytype')[0:5] == 'SOCKS':
                    logger.error('Bitmessage MIGHT be having trouble connecting to the SOCKS server. ' + str(err))
                else:
                    if err[0] == errno.ENETUNREACH:
                        state.networkProtocolAvailability[protocol.networkType(peer.host)] = False
                    if shared.verbose >= 1:
                        logger.debug('Could NOT connect to ' + str(peer) + 'during outgoing attempt. ' + str(err))

                deletedPeer = None
                with knownnodes.knownNodesLock:
                    """
                    It is remotely possible that peer is no longer in knownnodes.knownNodes.
                    This could happen if two outgoingSynSender threads both try to 
                    connect to the same peer, both fail, and then both try to remove
                    it from knownnodes.knownNodes. This is unlikely because of the
                    alreadyAttemptedConnectionsList but because we clear that list once
                    every half hour, it can happen.
                    """
                    if peer in knownnodes.knownNodes[self.streamNumber]:
                        timeLastSeen = knownnodes.knownNodes[self.streamNumber][peer]
                        if (int(time.time()) - timeLastSeen) > 172800 and len(knownnodes.knownNodes[self.streamNumber]) > 1000:  # for nodes older than 48 hours old if we have more than 1000 hosts in our list, delete from the knownnodes.knownNodes data-structure.
                            del knownnodes.knownNodes[self.streamNumber][peer]
                            deletedPeer = peer
                if deletedPeer:
                    logger.debug('deleting ' + str(peer) + ' from knownnodes.knownNodes because it is more than 48 hours old and we could not connect to it.')

            except Exception as err:
                logger.exception('An exception has occurred in the outgoingSynSender thread that was not caught by other exception types:')
            self.wait(0.1)
