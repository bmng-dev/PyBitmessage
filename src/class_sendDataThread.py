import errno
import logging
import socket
import threading
import time
from ssl import SSLError
from struct import pack

import protocol
import state
import throttle
from addresses import encodeVarint
from class_objectHashHolder import objectHashHolder
from inventory import PendingUpload


logger = logging.getLogger(__name__)


# Every connection to a peer has a sendDataThread (and also a
# receiveDataThread).
class sendDataThread(threading.Thread):

    def __init__(self, sendDataThreadQueue, sock, HOST, PORT, streamNumber):
        threading.Thread.__init__(self, name="sendData")
        self.sendDataThreadQueue = sendDataThreadQueue
        state.sendDataQueues.append(self.sendDataThreadQueue)
        self.data = ''
        self.objectHashHolderInstance = objectHashHolder(self.sendDataThreadQueue)
        self.objectHashHolderInstance.daemon = True
        self.objectHashHolderInstance.start()
        self.connectionIsOrWasFullyEstablished = False
        self.sock = sock
        self.peer = state.Peer(HOST, PORT)
        self.name = "sendData-" + self.peer.host.replace(":", ".") # log parser field separator
        self.streamNumber = []
        self.services = 0
        self.buffer = ""
        self.initiatedConnection = False
        self.remoteProtocolVersion = - \
            1  # This must be set using setRemoteProtocolVersion command which is sent through the self.sendDataThreadQueue queue.
        self.lastTimeISentData = int(
            time.time())  # If this value increases beyond five minutes ago, we'll send a pong message to keep the connection alive.
        if streamNumber == -1:  # This was an incoming connection.
            self.initiatedConnection = False
        else:
            self.initiatedConnection = True
        #logger.debug('The streamNumber of this sendDataThread (ID: ' + str(id(self)) + ') at setup() is' + str(self.streamNumber))


    def sendVersionMessage(self):
        datatosend = protocol.assembleVersionMessage(
            self.peer.host, self.peer.port, state.streamsInWhichIAmParticipating, not self.initiatedConnection)  # the IP and port of the remote host, and my streamNumber.

        logger.debug('Sending version packet: ' + repr(datatosend))

        try:
            self.sendBytes(datatosend)
        except Exception as err:
            # if not 'Bad file descriptor' in err:
            logger.error('sock.sendall error: %s\n' % err)
            
        self.versionSent = 1

    def sendBytes(self, data = ""):
        self.buffer += data
        if len(self.buffer) < throttle.SendThrottle().chunkSize and self.sendDataThreadQueue.qsize() > 1:
            return True

        while self.buffer and state.shutdown == 0:
            try:
                amountSent = self.sock.send(self.buffer[:throttle.SendThrottle().chunkSize])
            except socket.timeout:
                logger.error('TCP send timed out after %s secs', self.sock.gettimeout())
                return False
            except SSLError as e:
                logger.error('Connection error (SSL)', exc_info=(type(e), e, None))
                return False
            except socket.error as e:
                if e.errno in (errno.EAGAIN, errno.EINTR):
                    logger.debug('sock.recv retriable error')
                    continue
                if e.errno in (errno.EPIPE, errno.ECONNRESET, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.ECONNREFUSED):
                    logger.debug('Connection error: %s', str(e))
                    return False
                logger.exception('Send error')
                return False
            throttle.SendThrottle().wait(amountSent)
            self.lastTimeISentData = int(time.time())
            self.buffer = self.buffer[amountSent:]
        return True

    def run(self):
        logger.debug('sendDataThread starting. ID: ' + str(id(self)) + '. Number of queues in sendDataQueues: ' + str(len(state.sendDataQueues)))
        while self.sendBytes():
            deststream, command, data = self.sendDataThreadQueue.get()

            if deststream == 0 or deststream in self.streamNumber:
                if command == 'shutdown':
                    logger.debug('sendDataThread (associated with ' + str(self.peer) + ') ID: ' + str(id(self)) + ' shutting down now.')
                    break
                # When you receive an incoming connection, a sendDataThread is
                # created even though you don't yet know what stream number the
                # remote peer is interested in. They will tell you in a version
                # message and if you too are interested in that stream then you
                # will continue on with the connection and will set the
                # streamNumber of this send data thread here:
                elif command == 'setStreamNumber':
                    self.streamNumber = data
                    logger.debug('setting the stream number to %s', ', '.join(str(x) for x in self.streamNumber))
                elif command == 'setRemoteProtocolVersion':
                    specifiedRemoteProtocolVersion = data
                    logger.debug('setting the remote node\'s protocol version in the sendDataThread (ID: ' + str(id(self)) + ') to ' + str(specifiedRemoteProtocolVersion))
                    self.remoteProtocolVersion = specifiedRemoteProtocolVersion
                elif command == 'sendaddr':
                    if self.connectionIsOrWasFullyEstablished: # only send addr messages if we have sent and heard a verack from the remote node
                        numberOfAddressesInAddrMessage = len(data)
                        payload = ''
                        for hostDetails in data:
                            timeLastReceivedMessageFromThisNode, streamNumber, services, host, port = hostDetails
                            payload += pack(
                                '>Q', timeLastReceivedMessageFromThisNode)  # now uses 64-bit time
                            payload += pack('>I', streamNumber)
                            payload += pack(
                                '>q', services)  # service bit flags offered by this node
                            payload += protocol.encodeHost(host)
                            payload += pack('>H', port)
    
                        payload = encodeVarint(numberOfAddressesInAddrMessage) + payload
                        packet = protocol.CreatePacket('addr', payload)
                        if not self.sendBytes(packet):
                            break
                elif command == 'advertiseobject':
                    self.objectHashHolderInstance.holdHash(data)
                elif command == 'sendinv':
                    if self.connectionIsOrWasFullyEstablished: # only send inv messages if we have send and heard a verack from the remote node
                        payload = ''
                        for hash in data:
                            payload += hash
                        if payload != '':
                            payload = encodeVarint(len(payload)/32) + payload
                            packet = protocol.CreatePacket('inv', payload)
                            if not self.sendBytes(packet):
                                break
                elif command == 'pong':
                    if self.lastTimeISentData < (int(time.time()) - 298):
                        # Send out a pong message to keep the connection alive.
                        logger.debug('Sending pong to ' + str(self.peer) + ' to keep connection alive.')
                        packet = protocol.CreatePacket('pong')
                        if not self.sendBytes(packet):
                            break
                elif command == 'sendRawData':
                    objectHash = None
                    if type(data) in [list, tuple]:
                        objectHash, data = data
                    if not self.sendBytes(data):
                        break
                    if objectHash:
                        PendingUpload().delete(objectHash)
                elif command == 'connectionIsOrWasFullyEstablished':
                    self.connectionIsOrWasFullyEstablished = True
                    self.services, self.sock = data
            elif self.connectionIsOrWasFullyEstablished:
                logger.error('sendDataThread ID: ' + str(id(self)) + ' ignoring command ' + command + ' because the thread is not in stream ' + str(deststream) + ' but in streams ' + ', '.join(str(x) for x in self.streamNumber))
            self.sendDataThreadQueue.task_done()
        # Flush if the cycle ended with break
        try:
            self.sendDataThreadQueue.task_done()
        except ValueError:
            pass

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except:
            pass
        state.sendDataQueues.remove(self.sendDataThreadQueue)
        PendingUpload().threadEnd()
        logger.info('sendDataThread ending. ID: ' + str(id(self)) + '. Number of queues in sendDataQueues: ' + str(len(state.sendDataQueues)))
        self.objectHashHolderInstance.close()
