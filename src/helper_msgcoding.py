import string
import zlib

import msgpack

from bmconfigparser import BMConfigParser
from debug import logger
from tr import _translate

BITMESSAGE_ENCODING_IGNORE = 0
BITMESSAGE_ENCODING_TRIVIAL = 1
BITMESSAGE_ENCODING_SIMPLE = 2
BITMESSAGE_ENCODING_EXTENDED = 3

class DecompressionSizeException(Exception):
    def __init__(self, size):
        self.size = size


class MsgEncode(object):
    def __init__(self, message, encoding=BITMESSAGE_ENCODING_SIMPLE):
        self.data = None
        self.encoding = encoding
        self.length = 0
        if self.encoding == BITMESSAGE_ENCODING_EXTENDED:
            self.encodeExtended(message)
        elif self.encoding == BITMESSAGE_ENCODING_SIMPLE:
            self.encodeSimple(message)
        elif self.encoding == BITMESSAGE_ENCODING_TRIVIAL:
            self.encodeTrivial(message)
        else:
            raise ValueError("Unknown encoding %i" % (encoding))

    def encodeExtended(self, message):
        obj = {"": 'message'}
        try:
            obj["subject"] = message["subject"]
            obj["body"] = message["body"]
        except KeyError as e:
            logger.error("Missing key %s", e.name)

        try:
            self.data = zlib.compress(msgpack.dumps(obj), 9)
        except zlib.error:
            logger.error("Error compressing message")
            raise
        except msgpack.exceptions.PackException:
            logger.error("Error msgpacking message")
            raise
        self.length = len(self.data)

    def encodeSimple(self, message):
        self.data = 'Subject:' + message['subject'] + '\n' + 'Body:' + message['body']
        self.length = len(self.data)

    def encodeTrivial(self, message):
        self.data = message['body']
        self.length = len(self.data)


class MsgDecode(object):
    def __init__(self, encoding, data):
        self.encoding = encoding
        if self.encoding == BITMESSAGE_ENCODING_EXTENDED:
            self.decodeExtended(data)
        elif self.encoding in [BITMESSAGE_ENCODING_SIMPLE, BITMESSAGE_ENCODING_TRIVIAL]:
            self.decodeSimple(data)
        else:
            self.body = _translate("MsgDecode", "The message has an unknown encoding.\nPerhaps you should upgrade Bitmessage.")
            self.subject = _translate("MsgDecode", "Unknown encoding")

    def decodeExtended(self, data):
        dc = zlib.decompressobj()
        tmp = ""
        while len(tmp) <= BMConfigParser().safeGetInt("zlib", "maxsize"):
            try:
                got = dc.decompress(data, BMConfigParser().safeGetInt("zlib", "maxsize") + 1 - len(tmp))
                # EOF
                if got == "":
                    break
                tmp += got
                data = dc.unconsumed_tail
            except zlib.error:
                logger.error("Error decompressing message")
                raise
        else:
            raise DecompressionSizeException(len(tmp))

        try:
            obj = msgpack.loads(tmp)
        except (msgpack.exceptions.UnpackException,
                msgpack.exceptions.ExtraData):
            logger.error("Error msgunpacking message")
            raise

        try:
            msgType = obj[""]
        except KeyError:
            logger.error("Message type missing")
            raise

        if msgType != 'message':
            logger.error("Don't know how to handle message type: \"%s\"", msgType)
            raise ValueError("Malformed message")

        try:
            if type(obj["subject"]) is str:
                self.subject = unicode(obj["subject"], 'utf-8', 'replace')
            else:
                self.subject = unicode(str(obj["subject"]), 'utf-8', 'replace')
            if type(obj["body"]) is str:
                self.body = unicode(obj["body"], 'utf-8', 'replace')
            else:
                self.body = unicode(str(obj["body"]), 'utf-8', 'replace')
        except KeyError as e:
            logger.error("Missing mandatory key %s", e)
            raise ValueError("Malformed message")

    def decodeSimple(self, data):
        bodyPositionIndex = string.find(data, '\nBody:')
        if bodyPositionIndex > 1:
            subject = data[8:bodyPositionIndex]
            # Only save and show the first 500 characters of the subject.
            # Any more is probably an attack.
            subject = subject[:500]
            body = data[bodyPositionIndex + 6:]
        else:
            subject = ''
            body = data
        # Throw away any extra lines (headers) after the subject.
        if subject:
            subject = subject.splitlines()[0]
        self.subject = subject
        self.body = body
