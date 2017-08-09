doTimingAttackMitigation = False

import base64
import datetime
import errno
import hashlib
import logging
import math
import os
import Queue
import random
import socket
import ssl
import sys
import threading
import time
import traceback
from binascii import hexlify
from struct import unpack

import knownnodes
import paths
import protocol
import queues
import shared
import state
import throttle
import tr
from addresses import (calculateInventoryHash, decodeVarint, encodeVarint,
                       varintDecodeError)
from bmconfigparser import BMConfigParser
from class_objectHashHolder import objectHashHolder
from helper_generic import isHostInPrivateIPRange
from inventory import Inventory, PendingDownloadQueue, PendingUpload


logger = logging.getLogger(__name__)


class ProtocolError(Exception): pass

# This thread is created either by the synSenderThread(for outgoing
# connections) or the singleListenerThread(for incoming connections).

class receiveDataThread(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self, name="receiveData")
        self.data = ''
        self.verackSent = False
        self.verackReceived = False
        self.addrReceived = False

    def setup(
        self,
        sock,
        HOST,
        port,
        streamNumber,
        selfInitiatedConnections,
        sendDataThreadQueue,
        objectHashHolderInstance):
        
        self.sock = sock
        self.peer = state.Peer(HOST, port)
        self.name = "receiveData-" + self.peer.host.replace(":", ".") # ":" log parser field separator
        self.streamNumber = state.streamsInWhichIAmParticipating
        self.remoteStreams = []
        self.selfInitiatedConnections = selfInitiatedConnections
        self.sendDataThreadQueue = sendDataThreadQueue # used to send commands and data to the sendDataThread
        self.hostIdent = self.peer.port if ".onion" in BMConfigParser().get('bitmessagesettings', 'onionhostname') and protocol.checkSocksIP(self.peer.host) else self.peer.host
        shared.connectedHostsList[
            self.hostIdent] = 0  # The very fact that this receiveData thread exists shows that we are connected to the remote host. Let's add it to this list so that an outgoingSynSender thread doesn't try to connect to it.
        self.connectionIsOrWasFullyEstablished = False  # set to true after the remote node and I accept each other's version messages. This is needed to allow the user interface to accurately reflect the current number of connections.
        self.services = 0
        if streamNumber == -1:  # This was an incoming connection. Send out a version message if we accept the other node's version message.
            self.initiatedConnection = False
        else:
            self.initiatedConnection = True
            for stream in self.streamNumber:
                self.selfInitiatedConnections[stream][self] = 0
        self.objectHashHolderInstance = objectHashHolderInstance
        self.downloadQueue = PendingDownloadQueue()
        self.startTime = time.time()

    def run(self):
        logger.debug('receiveDataThread starting. ID ' + str(id(self)) + '. The size of the shared.connectedHostsList is now ' + str(len(shared.connectedHostsList)))

        while state.shutdown == 0:
            dataLen = len(self.data)
            try:
                dataRecv = self.sock.recv(throttle.ReceiveThrottle().chunkSize)
                self.data += dataRecv
                throttle.ReceiveThrottle().wait(len(dataRecv))
            except socket.timeout:
                logger.error('TCP receive timed out after %s secs', self.sock.gettimeout())
                break
            except ssl.SSLError as err:
                if err.message and err.message.endswith('timed out'):
                    logger.error('TLS receive timed out after %s secs', self.sock.gettimeout())
                else:
                    logger.error ('SSL error: %i/%s', err.errno if err.errno else 0, str(err))
                break
            except socket.error as err:
                if err.errno in (errno.EAGAIN, errno.EINTR):
                    logger.debug('sock.recv retriable error')
                    continue
                logger.error('sock.recv error. Closing receiveData thread, %s', str(err))
                break
            # print 'Received', repr(self.data)
            if len(self.data) == dataLen: # If self.sock.recv returned no data:
                logger.debug('Connection to ' + str(self.peer) + ' closed. Closing receiveData thread')
                break
            try:
                while self.processData():
                    pass
            except ProtocolError as ex:
                logger.error('Protocol violation: %s', ex.message)
                break

        try:
            for stream in self.streamNumber:
                try:
                    del self.selfInitiatedConnections[stream][self]
                except KeyError:
                    pass
            logger.debug('removed self (a receiveDataThread) from selfInitiatedConnections')
        except:
            pass
        self.sendDataThreadQueue.put((0, 'shutdown','no data')) # commands the corresponding sendDataThread to shut itself down.
        try:
            del shared.connectedHostsList[self.hostIdent]
        except Exception as err:
            logger.error('Could not delete ' + str(self.hostIdent) + ' from shared.connectedHostsList.' + str(err))

        queues.UISignalQueue.put(('updateNetworkStatusTab', 'no data'))
        self.checkTimeOffsetNotification()
        logger.debug('receiveDataThread ending. ID ' + str(id(self)) + '. The size of the shared.connectedHostsList is now ' + str(len(shared.connectedHostsList)))

    def antiIntersectionDelay(self, initial = False):
        # estimated time for a small object to propagate across the whole network
        delay = math.ceil(math.log(max(len(knownnodes.knownNodes[x]) for x in knownnodes.knownNodes) + 2, 20)) * (0.2 + objectHashHolder.size/2)
        # take the stream with maximum amount of nodes
        # +2 is to avoid problems with log(0) and log(1)
        # 20 is avg connected nodes count
        # 0.2 is avg message transmission time
        now = time.time()
        if initial and now - delay < self.startTime:
            logger.debug("Initial sleeping for %.2fs", delay - (now - self.startTime))
            time.sleep(delay - (now - self.startTime))
        elif not initial:
            logger.debug("Sleeping due to missing object for %.2fs", delay)
            time.sleep(delay)

    def checkTimeOffsetNotification(self):
        if shared.timeOffsetWrongCount >= 4 and not self.connectionIsOrWasFullyEstablished:
            queues.UISignalQueue.put(('updateStatusBar', tr._translate("MainWindow", "The time on your computer, %1, may be wrong. Please verify your settings.").arg(datetime.datetime.now().strftime("%H:%M:%S"))))

    def processData(self):
        if len(self.data) < protocol.Header.size:  # if so little of the data has arrived that we can't even read the checksum then wait for more data.
            return False
        
        magic,command,payloadLength,checksum = protocol.Header.unpack(self.data[:protocol.Header.size])
        if magic != 0xE9BEB4D9:
            raise ProtocolError('Incorrect magic')
        if payloadLength > 1600100: # ~1.6 MB which is the maximum possible size of an inv message.
            raise ProtocolError('Oversized payload')
        if len(self.data) < payloadLength + protocol.Header.size:  # check if the whole message has arrived yet.
            return False
        payload = self.data[protocol.Header.size:payloadLength + protocol.Header.size]
        if checksum != hashlib.sha512(payload).digest()[0:4]:  # test the checksum in the message.
            logger.error('Checksum incorrect. Clearing this message.')
            self.data = self.data[payloadLength + protocol.Header.size:]
            return True

        # The time we've last seen this node is obviously right now since we
        # just received valid data from it. So update the knownNodes list so
        # that other peers can be made aware of its existance.
        if self.initiatedConnection and self.connectionIsOrWasFullyEstablished:  # The remote port is only something we should share with others if it is the remote node's incoming port (rather than some random operating-system-assigned outgoing port).
            with knownnodes.knownNodesLock:
                for stream in self.streamNumber:
                    knownnodes.knownNodes[stream][self.peer] = int(time.time())
        
        #Strip the nulls
        command = command.rstrip('\x00')
        logger.debug('remoteCommand ' + repr(command) + ' from ' + str(self.peer))
        
        try:
            #TODO: Use a dispatcher here
            if command == 'error':
                self.recerror(payload)
            elif not self.connectionIsOrWasFullyEstablished:
                if command == 'version':
                    self.recversion(payload)
                elif command == 'verack':
                    self.recverack()
            else:
                if command == 'addr':
                    self.recaddr(payload)
                elif command == 'inv':
                    self.recinv(payload)
                elif command == 'getdata':
                    self.recgetdata(payload)
                elif command == 'object':
                    self.recobject(payload)
                elif command == 'ping':
                    self.sendpong(payload)
                elif command == 'pong':
                    pass
                else:
                    logger.info("Unknown command %s, ignoring", command)
        except varintDecodeError as e:
            logger.debug("There was a problem with a varint while processing a message from the wire. Some details: %s" % e)
        except Exception as e:
            logger.critical("Critical error in a receiveDataThread: \n%s" % traceback.format_exc())
        
        self.data = self.data[payloadLength + protocol.Header.size:] # take this message out and then process the next message

        if self.data == '': # if there are no more messages
            toRequest = []
            try:
                for i in range(len(self.downloadQueue.pending), 100):
                    while True:
                        hashId = self.downloadQueue.get(False)
                        if not hashId in Inventory():
                            toRequest.append(hashId)
                            break
                        # don't track download for duplicates
                        self.downloadQueue.task_done(hashId)
            except Queue.Empty:
                pass
            if len(toRequest) > 0:
                self.sendgetdata(toRequest)
            return False
        return True

    def sendpong(self, payload):
        logger.debug('Sending pong')
        self.sendDataThreadQueue.put((0, 'sendRawData', protocol.CreatePacket('pong', payload)))

    def recverack(self):
        logger.debug('verack received')
        self.verackReceived = True
        if self.verackSent:
            # We have thus both sent and received a verack.
            self.connectionFullyEstablished()

    def sslHandshake(self):
        logger.debug("Initialising TLS")
        if sys.version_info >= (2,7,9):
            context = ssl.SSLContext(protocol.sslProtocolVersion)
            context.set_ciphers(protocol.sslProtocolCiphers)
            context.set_ecdh_curve("secp256k1")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            # also exclude TLSv1 and TLSv1.1 in the future
            context.options = ssl.OP_ALL | ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_SINGLE_ECDH_USE | ssl.OP_CIPHER_SERVER_PREFERENCE
            self.sock = context.wrap_socket(self.sock, server_side = not self.initiatedConnection, do_handshake_on_connect=False)
        else:
            self.sock = ssl.wrap_socket(self.sock, keyfile = os.path.join(paths.codePath(), 'sslkeys', 'key.pem'), certfile = os.path.join(paths.codePath(), 'sslkeys', 'cert.pem'), server_side = not self.initiatedConnection, ssl_version=protocol.sslProtocolVersion, do_handshake_on_connect=False, ciphers=protocol.sslProtocolCiphers)
        self.sendDataThreadQueue.join()

        self.sock.do_handshake()
        cipher, definition, strength = self.sock.cipher()
        logger.debug("TLS handshake success - %s defined in %s using %d secret bits", cipher, definition, strength)
        if sys.version_info >= (2, 7, 9):
            logger.debug("TLS protocol version: %s", self.sock.version())

    def peerValidityChecks(self):
        if self.remoteProtocolVersion < 3:
            self.sendDataThreadQueue.put((0, 'sendRawData',protocol.assembleErrorMessage(
                fatal=2, errorText="Your is using an old protocol. Closing connection.")))
            logger.debug ('Closing connection to old protocol version ' + str(self.remoteProtocolVersion) + ' node: ' + str(self.peer))
            return False
        if self.timeOffset > 3600:
            self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleErrorMessage(
                fatal=2, errorText="Your time is too far in the future compared to mine. Closing connection.")))
            logger.info("%s's time is too far in the future (%s seconds). Closing connection to it.", self.peer, self.timeOffset)
            shared.timeOffsetWrongCount += 1
            time.sleep(2)
            return False
        elif self.timeOffset < -3600:
            self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleErrorMessage(
                fatal=2, errorText="Your time is too far in the past compared to mine. Closing connection.")))
            logger.info("%s's time is too far in the past (timeOffset %s seconds). Closing connection to it.", self.peer, self.timeOffset)
            shared.timeOffsetWrongCount += 1
            return False
        else:
            shared.timeOffsetWrongCount = 0
        if len(self.streamNumber) == 0:
            self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleErrorMessage(
                fatal=2, errorText="We don't have shared stream interests. Closing connection.")))
            logger.debug ('Closed connection to ' + str(self.peer) + ' because there is no overlapping interest in streams.')
            return False
        return True
        
    def connectionFullyEstablished(self):
        if self.connectionIsOrWasFullyEstablished:
            # there is no reason to run this function a second time
            return

        if ((self.services & protocol.NODE_SSL == protocol.NODE_SSL) and
            protocol.haveSSL(not self.initiatedConnection)):
            try:
                self.sslHandshake()
            except socket.error as ex:
                logger.error("SSL socket handshake failed, shutting down connection", exc_info=(type(ex), ex, None))
                self.sendDataThreadQueue.put((0, 'shutdown', None))
                return

        if self.peerValidityChecks() == False:
            time.sleep(2)
            self.sendDataThreadQueue.put((0, 'shutdown','no data'))
            self.checkTimeOffsetNotification()
            return

        self.connectionIsOrWasFullyEstablished = True
        shared.timeOffsetWrongCount = 0

        # Command the corresponding sendDataThread to set its own connectionIsOrWasFullyEstablished variable to True also
        self.sendDataThreadQueue.put((0, 'connectionIsOrWasFullyEstablished', (self.services, self.sock)))

        if not self.initiatedConnection:
            shared.clientHasReceivedIncomingConnections = True
            queues.UISignalQueue.put(('setStatusIcon', 'green'))
        self.sock.settimeout(
            600)  # We'll send out a ping every 5 minutes to make sure the connection stays alive if there has been no other traffic to send lately.
        queues.UISignalQueue.put(('updateNetworkStatusTab', 'no data'))
        logger.debug('Connection fully established with ' + str(self.peer) + "\n" + \
            'The size of the connectedHostsList is now ' + str(len(shared.connectedHostsList)) + "\n" + \
            'The length of sendDataQueues is now: ' + str(len(state.sendDataQueues)) + "\n" + \
            'broadcasting addr from within connectionFullyEstablished function.')

        if self.initiatedConnection:
            state.networkProtocolAvailability[protocol.networkType(self.peer.host)] = True

        # we need to send our own objects to this node
        PendingUpload().add()

        self.sendaddr()  # This is one large addr message to this one peer.
        if len(shared.connectedHostsList) > \
            BMConfigParser().safeGetInt("bitmessagesettings", "maxtotalconnections", 200):
            logger.info ('We are connected to too many people. Closing connection.')
            if self.initiatedConnection:
                self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleErrorMessage(fatal=2, errorText="Thank you for providing a listening node.")))
            else:
                self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleErrorMessage(fatal=2, errorText="Server full, please try again later.")))
            self.sendDataThreadQueue.put((0, 'shutdown','no data'))
            return
        self.sendBigInv()

    def sendBigInv(self):
        # Select all hashes for objects in this stream.
        bigInvList = {}
        for stream in self.streamNumber:
            for hash in Inventory().unexpired_hashes_by_stream(stream):
                if not self.objectHashHolderInstance.hasHash(hash):
                    bigInvList[hash] = 0
        numberOfObjectsInInvMessage = 0
        payload = ''
        # Now let us start appending all of these hashes together. They will be
        # sent out in a big inv message to our new peer.
        for hash, storedValue in bigInvList.items():
            payload += hash
            numberOfObjectsInInvMessage += 1
            if numberOfObjectsInInvMessage == 50000:  # We can only send a max of 50000 items per inv message but we may have more objects to advertise. They must be split up into multiple inv messages.
                self.sendinvMessageToJustThisOnePeer(
                    numberOfObjectsInInvMessage, payload)
                payload = ''
                numberOfObjectsInInvMessage = 0
        if numberOfObjectsInInvMessage > 0:
            self.sendinvMessageToJustThisOnePeer(
                numberOfObjectsInInvMessage, payload)

    # Used to send a big inv message when the connection with a node is 
    # first fully established. Notice that there is also a broadcastinv 
    # function for broadcasting invs to everyone in our stream.
    def sendinvMessageToJustThisOnePeer(self, numberOfObjects, payload):
        payload = encodeVarint(numberOfObjects) + payload
        logger.debug('Sending huge inv message with ' + str(numberOfObjects) + ' objects to just this one peer')
        self.sendDataThreadQueue.put((0, 'sendRawData', protocol.CreatePacket('inv', payload)))

    def _sleepForTimingAttackMitigation(self, sleepTime):
        # We don't need to do the timing attack mitigation if we are
        # only connected to the trusted peer because we can trust the
        # peer not to attack
        if sleepTime > 0 and doTimingAttackMitigation and state.trustedPeer == None:
            logger.debug('Timing attack mitigation: Sleeping for ' + str(sleepTime) + ' seconds.')
            time.sleep(sleepTime)
            
    def recerror(self, data):
        """
        The remote node has been polite enough to send you an error message.
        """
        fatalStatus, readPosition = decodeVarint(data[:10])
        banTime, banTimeLength = decodeVarint(data[readPosition:readPosition+10])
        readPosition += banTimeLength
        inventoryVectorLength, inventoryVectorLengthLength = decodeVarint(data[readPosition:readPosition+10])
        if inventoryVectorLength > 100:
            return
        readPosition += inventoryVectorLengthLength
        inventoryVector = data[readPosition:readPosition+inventoryVectorLength]
        readPosition += inventoryVectorLength
        errorTextLength, errorTextLengthLength = decodeVarint(data[readPosition:readPosition+10])
        if errorTextLength > 1000:
            return 
        readPosition += errorTextLengthLength
        errorText = data[readPosition:readPosition+errorTextLength]
        if fatalStatus == 0:
            fatalHumanFriendly = 'Warning'
        elif fatalStatus == 1:
            fatalHumanFriendly = 'Error'
        elif fatalStatus == 2:
            fatalHumanFriendly = 'Fatal'
        message = '%s message received from %s: %s.' % (fatalHumanFriendly, self.peer, errorText)
        if inventoryVector:
            message += " This concerns object %s" % hexlify(inventoryVector)
        if banTime > 0:
            message += " Remote node says that the ban time is %s" % banTime
        logger.error(message)


    def recobject(self, data):
        self.messageProcessingStartTime = time.time()
        lengthOfTimeWeShouldUseToProcessThisMessage = shared.checkAndShareObjectWithPeers(data)
        self.downloadQueue.task_done(calculateInventoryHash(data))
        
        """
        Sleeping will help guarantee that we can process messages faster than a 
        remote node can send them. If we fall behind, the attacker could observe 
        that we are are slowing down the rate at which we request objects from the
        network which would indicate that we own a particular address (whichever
        one to which they are sending all of their attack messages). Note
        that if an attacker connects to a target with many connections, this
        mitigation mechanism might not be sufficient.
        """
        sleepTime = lengthOfTimeWeShouldUseToProcessThisMessage - (time.time() - self.messageProcessingStartTime)
        self._sleepForTimingAttackMitigation(sleepTime)
    

    # We have received an inv message
    def recinv(self, data):
        numberOfItemsInInv, lengthOfVarint = decodeVarint(data[:10])
        if numberOfItemsInInv > 50000:
            sys.stderr.write('Too many items in inv message!')
            return
        if len(data) < lengthOfVarint + (numberOfItemsInInv * 32):
            logger.info('inv message doesn\'t contain enough data. Ignoring.')
            return

        startTime = time.time()
        advertisedSet = set()
        for i in range(numberOfItemsInInv):
            advertisedSet.add(data[lengthOfVarint + (32 * i):32 + lengthOfVarint + (32 * i)])
        objectsNewToMe = advertisedSet
        for stream in self.streamNumber:
            objectsNewToMe -= Inventory().hashes_by_stream(stream)
        logger.info('inv message lists %s objects. Of those %s are new to me. It took %s seconds to figure that out.', numberOfItemsInInv, len(objectsNewToMe), time.time()-startTime)
        for item in random.sample(objectsNewToMe, len(objectsNewToMe)):
            self.downloadQueue.put(item)

    # Send a getdata message to our peer to request the object with the given
    # hash
    def sendgetdata(self, hashes):
        if len(hashes) == 0:
            return
        logger.debug('sending getdata to retrieve %i objects', len(hashes))
        payload = encodeVarint(len(hashes)) + ''.join(hashes)
        self.sendDataThreadQueue.put((0, 'sendRawData', protocol.CreatePacket('getdata', payload)), False)


    # We have received a getdata request from our peer
    def recgetdata(self, data):
        numberOfRequestedInventoryItems, lengthOfVarint = decodeVarint(
            data[:10])
        if len(data) < lengthOfVarint + (32 * numberOfRequestedInventoryItems):
            logger.debug('getdata message does not contain enough data. Ignoring.')
            return
        self.antiIntersectionDelay(True) # only handle getdata requests if we have been connected long enough
        for i in xrange(numberOfRequestedInventoryItems):
            hash = data[lengthOfVarint + (
                i * 32):32 + lengthOfVarint + (i * 32)]
            logger.debug('received getdata request for item:' + hexlify(hash))

            if self.objectHashHolderInstance.hasHash(hash):
                self.antiIntersectionDelay()
            else:
                if hash in Inventory():
                    self.sendObject(hash, Inventory()[hash].payload)
                else:
                    self.antiIntersectionDelay()
                    logger.warning('%s asked for an object with a getdata which is not in either our memory inventory or our SQL inventory. We probably cleaned it out after advertising it but before they got around to asking for it.' % (self.peer,))

    # Our peer has requested (in a getdata message) that we send an object.
    def sendObject(self, hash, payload):
        logger.debug('sending an object.')
        self.sendDataThreadQueue.put((0, 'sendRawData', (hash, protocol.CreatePacket('object',payload))))

    def _checkIPAddress(self, host):
        if host[0:12] == '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xFF\xFF':
            hostStandardFormat = socket.inet_ntop(socket.AF_INET, host[12:])
            return self._checkIPv4Address(host[12:], hostStandardFormat)
        elif host[0:6] == '\xfd\x87\xd8\x7e\xeb\x43':
            # Onion, based on BMD/bitcoind
            hostStandardFormat = base64.b32encode(host[6:]).lower() + ".onion"
            return hostStandardFormat
        else:
            hostStandardFormat = socket.inet_ntop(socket.AF_INET6, host)
            if hostStandardFormat == "":
                # This can happen on Windows systems which are not 64-bit compatible 
                # so let us drop the IPv6 address. 
                return False
            return self._checkIPv6Address(host, hostStandardFormat)

    def _checkIPv4Address(self, host, hostStandardFormat):
        if host[0] == '\x7F': # 127/8
            logger.debug('Ignoring IP address in loopback range: ' + hostStandardFormat)
            return False
        if host[0] == '\x0A': # 10/8
            logger.debug('Ignoring IP address in private range: ' + hostStandardFormat)
            return False
        if host[0:2] == '\xC0\xA8': # 192.168/16
            logger.debug('Ignoring IP address in private range: ' + hostStandardFormat)
            return False
        if host[0:2] >= '\xAC\x10' and host[0:2] < '\xAC\x20': # 172.16/12
            logger.debug('Ignoring IP address in private range:' + hostStandardFormat)
            return False
        return hostStandardFormat

    def _checkIPv6Address(self, host, hostStandardFormat):
        if host == ('\x00' * 15) + '\x01':
            logger.debug('Ignoring loopback address: ' + hostStandardFormat)
            return False
        if host[0] == '\xFE' and (ord(host[1]) & 0xc0) == 0x80:
            logger.debug ('Ignoring local address: ' + hostStandardFormat)
            return False
        if (ord(host[0]) & 0xfe) == 0xfc:
            logger.debug ('Ignoring unique local address: ' + hostStandardFormat)
            return False
        return hostStandardFormat

    # We have received an addr message.
    def recaddr(self, data):
        if self.addrReceived:
            logger.debug('Ignoring extraneous addr (%d bytes)', len(data))
            return

        self.addrReceived = True

        numberOfAddressesIncluded, lengthOfNumberOfAddresses = decodeVarint(
            data[:10])

        if shared.verbose >= 1:
            logger.debug('addr message contains ' + str(numberOfAddressesIncluded) + ' IP addresses.')

        if numberOfAddressesIncluded > 1000 or numberOfAddressesIncluded == 0:
            return
        if len(data) != lengthOfNumberOfAddresses + (38 * numberOfAddressesIncluded):
            logger.debug('addr message does not contain the correct amount of data. Ignoring.')
            return

        for i in range(0, numberOfAddressesIncluded):
            fullHost = data[20 + lengthOfNumberOfAddresses + (38 * i):36 + lengthOfNumberOfAddresses + (38 * i)]
            recaddrStream, = unpack('>I', data[8 + lengthOfNumberOfAddresses + (
                38 * i):12 + lengthOfNumberOfAddresses + (38 * i)])
            if recaddrStream == 0:
                continue
            if recaddrStream not in self.streamNumber and (recaddrStream / 2) not in self.streamNumber:  # if the embedded stream number and its parent are not in my streams then ignore it. Someone might be trying funny business.
                continue
            recaddrServices, = unpack('>Q', data[12 + lengthOfNumberOfAddresses + (
                38 * i):20 + lengthOfNumberOfAddresses + (38 * i)])
            recaddrPort, = unpack('>H', data[36 + lengthOfNumberOfAddresses + (
                38 * i):38 + lengthOfNumberOfAddresses + (38 * i)])
            hostStandardFormat = self._checkIPAddress(fullHost)
            if hostStandardFormat is False:
                continue
            if recaddrPort == 0:
                continue
            timeSomeoneElseReceivedMessageFromThisNode, = unpack('>Q', data[lengthOfNumberOfAddresses + (
                38 * i):8 + lengthOfNumberOfAddresses + (38 * i)])  # This is the 'time' value in the received addr message. 64-bit.
            if recaddrStream not in knownnodes.knownNodes:  # knownNodes is a dictionary of dictionaries with one outer dictionary for each stream. If the outer stream dictionary doesn't exist yet then we must make it.
                with knownnodes.knownNodesLock:
                    knownnodes.knownNodes[recaddrStream] = {}
            peerFromAddrMessage = state.Peer(hostStandardFormat, recaddrPort)
            if peerFromAddrMessage not in knownnodes.knownNodes[recaddrStream]:
                # only if recent
                if timeSomeoneElseReceivedMessageFromThisNode > (int(time.time()) - 10800) and timeSomeoneElseReceivedMessageFromThisNode < (int(time.time()) + 10800):
                    # bootstrap provider?
                    if BMConfigParser().safeGetInt('bitmessagesettings', 'maxoutboundconnections') >= \
                        BMConfigParser().safeGetInt('bitmessagesettings', 'maxtotalconnections', 200):
                        knownnodes.trimKnownNodes(recaddrStream)
                        with knownnodes.knownNodesLock:
                            knownnodes.knownNodes[recaddrStream][peerFromAddrMessage] = int(time.time()) - 86400 # penalise initially by 1 day
                        logger.debug('added new node ' + str(peerFromAddrMessage) + ' to knownNodes in stream ' + str(recaddrStream))
                        shared.needToWriteKnownNodesToDisk = True
                    # normal mode
                    elif len(knownnodes.knownNodes[recaddrStream]) < 20000:
                        with knownnodes.knownNodesLock:
                            knownnodes.knownNodes[recaddrStream][peerFromAddrMessage] = timeSomeoneElseReceivedMessageFromThisNode
                        logger.debug('added new node ' + str(peerFromAddrMessage) + ' to knownNodes in stream ' + str(recaddrStream))
                        shared.needToWriteKnownNodesToDisk = True
            # only update if normal mode
            elif BMConfigParser().safeGetInt('bitmessagesettings', 'maxoutboundconnections') < \
                BMConfigParser().safeGetInt('bitmessagesettings', 'maxtotalconnections', 200):
                timeLastReceivedMessageFromThisNode = knownnodes.knownNodes[recaddrStream][
                    peerFromAddrMessage]
                if (timeLastReceivedMessageFromThisNode < timeSomeoneElseReceivedMessageFromThisNode) and (timeSomeoneElseReceivedMessageFromThisNode < int(time.time())+900): # 900 seconds for wiggle-room in case other nodes' clocks aren't quite right.
                    with knownnodes.knownNodesLock:
                        knownnodes.knownNodes[recaddrStream][peerFromAddrMessage] = timeSomeoneElseReceivedMessageFromThisNode

        for stream in self.streamNumber:
            logger.debug('knownNodes currently has %i nodes for stream %i', len(knownnodes.knownNodes[stream]), stream)


    # Send a huge addr message to our peer. This is only used 
    # when we fully establish a connection with a 
    # peer (with the full exchange of version and verack 
    # messages).
    def sendaddr(self):
        MAX_SUBSTREAM_ADDR = 500

        # protocol defines this as a maximum
        MAX_ADDR = 1000

        def itersubstreams():
            for stream in self.streamNumber:
                s = stream * 2
                yield s
                yield s + 1
        streams = set(self.streamNumber)
        substreams = set(itersubstreams()) - streams

        threshold = int(time.time()) - shared.maximumAgeOfNodesThatIAdvertiseToOthers
        def iternodes(streams):
            with knownnodes.knownNodesLock:
                for stream, nodes in knownnodes.knownNodes.iteritems():
                    if stream not in streams:
                        continue
                    for (host, port), timestamp in nodes.iteritems():
                        if timestamp < threshold:
                            continue
                        yield (timestamp, stream, 1, host, port)
        nodes = list(iternodes(substreams))
        nodes = random.sample(nodes, min(len(nodes), MAX_SUBSTREAM_ADDR))

        nodes2 = list(iternodes(streams))
        nodes += random.sample(nodes2, min(len(nodes2), MAX_ADDR - len(nodes)))
        nodes = random.sample(nodes, len(nodes))

        self.sendDataThreadQueue.put((0, 'sendaddr', nodes))

    # We have received a version message
    def recversion(self, data):
        if len(data) < 83:
            # This version message is unreasonably short. Forget it.
            return
        if self.verackSent:
            """
            We must have already processed the remote node's version message.
            There might be a time in the future when we Do want to process
            a new version message, like if the remote node wants to update
            the streams in which they are interested. But for now we'll
            ignore this version message
            """ 
            return

        self.remoteProtocolVersion, = unpack('>L', data[:4])
        self.services, = unpack('>q', data[4:12])

        timestamp, = unpack('>Q', data[12:20])
        self.timeOffset = timestamp - int(time.time())

        self.myExternalIP = socket.inet_ntoa(data[40:44])
        # print 'myExternalIP', self.myExternalIP
        self.remoteNodeIncomingPort, = unpack('>H', data[70:72])
        # print 'remoteNodeIncomingPort', self.remoteNodeIncomingPort
        useragentLength, lengthOfUseragentVarint = decodeVarint(
            data[80:84])
        readPosition = 80 + lengthOfUseragentVarint
        self.userAgent = data[readPosition:readPosition + useragentLength]

        readPosition += useragentLength
        numberOfStreamsInVersionMessage, lengthOfNumberOfStreamsInVersionMessage = decodeVarint(
            data[readPosition:])
        readPosition += lengthOfNumberOfStreamsInVersionMessage
        self.remoteStreams = []
        for i in range(numberOfStreamsInVersionMessage):
            newStreamNumber, lengthOfRemoteStreamNumber = decodeVarint(data[readPosition:])
            readPosition += lengthOfRemoteStreamNumber
            self.remoteStreams.append(newStreamNumber)
        logger.debug('Remote node useragent: %s, streams: (%s), time offset: %is.',
            self.userAgent, ', '.join(str(x) for x in self.remoteStreams), self.timeOffset)

        # find shared streams
        self.streamNumber = sorted(set(state.streamsInWhichIAmParticipating).intersection(self.remoteStreams))

        shared.connectedHostsList[
            self.hostIdent] = 1  # We use this data structure to not only keep track of what hosts we are connected to so that we don't try to connect to them again, but also to list the connections count on the Network Status tab.
        self.sendDataThreadQueue.put((0, 'setStreamNumber', self.remoteStreams))
        self.remoteNodeId, = unpack('>Q', data[72:80])
        if self.remoteNodeId == protocol.ServerNodeId:
            self.sendDataThreadQueue.put((0, 'shutdown','no data'))
            logger.debug('Closing connection to myself: ' + str(self.peer))
            return
        
        # The other peer's protocol version is of interest to the sendDataThread but we learn of it
        # in this version message. Let us inform the sendDataThread.
        self.sendDataThreadQueue.put((0, 'setRemoteProtocolVersion', self.remoteProtocolVersion))

        if not isHostInPrivateIPRange(self.peer.host):
            with knownnodes.knownNodesLock:
                for stream in self.remoteStreams:
                    knownnodes.knownNodes[stream][state.Peer(self.peer.host, self.remoteNodeIncomingPort)] = int(time.time())
                    if not self.initiatedConnection:
                        # bootstrap provider?
                        if BMConfigParser().safeGetInt('bitmessagesettings', 'maxoutboundconnections') >= \
                            BMConfigParser().safeGetInt('bitmessagesettings', 'maxtotalconnections', 200):
                            knownnodes.knownNodes[stream][state.Peer(self.peer.host, self.remoteNodeIncomingPort)] -= 10800 # penalise inbound, 3 hours
                        else:
                            knownnodes.knownNodes[stream][state.Peer(self.peer.host, self.remoteNodeIncomingPort)] -= 7200 # penalise inbound, 2 hours
                    shared.needToWriteKnownNodesToDisk = True

        self.sendverack()
        if self.initiatedConnection == False:
            self.sendversion()

    # Sends a version message
    def sendversion(self):
        logger.debug('Sending version message')
        self.sendDataThreadQueue.put((0, 'sendRawData', protocol.assembleVersionMessage(
                self.peer.host, self.peer.port, state.streamsInWhichIAmParticipating, not self.initiatedConnection)))

    # Sends a verack message
    def sendverack(self):
        logger.debug('Sending verack')
        self.sendDataThreadQueue.put((0, 'sendRawData', protocol.CreatePacket('verack')))
        self.verackSent = True
        if self.verackReceived:
            self.connectionFullyEstablished()
