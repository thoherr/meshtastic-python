"""
## an API for Meshtastic devices

Primary class: StreamInterface
Install with pip: "[pip3 install meshtastic](https://pypi.org/project/meshtastic/)"
Source code on [github](https://github.com/meshtastic/Meshtastic-python)

properties of StreamInterface:

- radioConfig - Current radio configuration and device settings, if you write to this the new settings will be applied to 
the device.
- nodes - The database of received nodes.  Includes always up-to-date location and username information for each 
node in the mesh.  This is a read-only datastructure.
- myNodeInfo - Contains read-only information about the local radio device (software version, hardware version, etc)

## Published PubSub topics

We use a [publish-subscribe](https://pypubsub.readthedocs.io/en/v4.0.3/) model to communicate asynchronous events.  Available
topics:

- meshtastic.connection.established - published once we've successfully connected to the radio and downloaded the node DB
- meshtastic.connection.lost - published once we've lost our link to the radio
- meshtastic.receive.position(packet) - delivers a received packet as a dictionary, if you only care about a particular 
type of packet, you should subscribe to the full topic name.  If you want to see all packets, simply subscribe to "meshtastic.receive".
- meshtastic.receive.user(packet)
- meshtastic.receive.data(packet)
- meshtastic.node.updated(node = NodeInfo) - published when a node in the DB changes (appears, location changed, username changed, etc...)

## Example Usage
```
import meshtastic
from pubsub import pub

def onReceive(packet): # called when a packet arrives
    print(f"Received: {packet}")

def onConnection(): # called when we (re)connect to the radio
    interface.sendText("hello mesh") # defaults to broadcast, specify a destination ID if you wish

pub.subscribe(onReceive, "meshtastic.receive")
pub.subscribe(onConnection, "meshtastic.connection.established")
interface = meshtastic.StreamInterface() # By default will try to find a meshtastic device, otherwise provide a device path like /dev/ttyUSB0

```

"""

import google.protobuf.json_format
import serial
import threading
import logging
import sys
import traceback
from . import mesh_pb2
from . import util
from pubsub import pub
from dotmap import DotMap

START1 = 0x94
START2 = 0xc3
HEADER_LEN = 4
MAX_TO_FROM_RADIO_SIZE = 512

BROADCAST_ADDR = "all"  # A special ID that means broadcast
BROADCAST_NUM = 255

MY_CONFIG_ID = 42


class MeshInterface:
    """Interface class for meshtastic devices

    Properties:

    isConnected
    nodes
    debugOut
    """

    def __init__(self, debugOut=None):
        """Constructor"""
        self.debugOut = debugOut
        self.nodes = None  # FIXME
        self.isConnected = False
        self._startConfig()

    def sendText(self, text, destinationId=BROADCAST_ADDR):
        """Send a utf8 string to some other node, if the node has a display it will also be shown on the device.

        Arguments:
            text {string} -- The text to send

        Keyword Arguments:
            destinationId {nodeId or nodeNum} -- where to send this message (default: {BROADCAST_ADDR})
        """
        self.sendData(text.encode("utf-8"), destinationId,
                      dataType=mesh_pb2.Data.CLEAR_TEXT)

    def sendData(self, byteData, destinationId=BROADCAST_ADDR, dataType=mesh_pb2.Data.OPAQUE):
        """Send a data packet to some other node"""
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.payload.data.payload = byteData
        meshPacket.payload.data.typ = dataType
        self.sendPacket(meshPacket, destinationId)

    def sendPacket(self, meshPacket, destinationId=BROADCAST_ADDR):
        """Send a MeshPacket to the specified node (or if unspecified, broadcast). 
        You probably don't want this - use sendData instead."""
        toRadio = mesh_pb2.ToRadio()
        # FIXME add support for non broadcast addresses

        if isinstance(destinationId, int):
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = 255
        else:
            nodeNum = self.nodes[destinationId].num

        meshPacket.to = nodeNum
        toRadio.packet.CopyFrom(meshPacket)
        self._sendToRadio(toRadio)

    def writeConfig(self):
        """Write the current (edited) radioConfig to the device"""
        if self.radioConfig == None:
            raise Exception("No RadioConfig has been read")

        t = mesh_pb2.ToRadio()
        t.set_radio.CopyFrom(self.radioConfig)
        self._sendToRadio(t)

    def _disconnected(self):
        """Called by subclasses to tell clients this interface has disconnected"""
        self.isConnected = False
        pub.sendMessage("meshtastic.connection.lost", interface=self)

    def _connected(self):
        """Called by this class to tell clients we are now fully connected to a node
        """
        self.isConnected = True
        pub.sendMessage("meshtastic.connection.established", interface=self)

    def _startConfig(self):
        """Start device packets flowing"""
        self.myInfo = None
        self.nodes = {}  # nodes keyed by ID
        self._nodesByNum = {}  # nodes keyed by nodenum
        self.radioConfig = None

        startConfig = mesh_pb2.ToRadio()
        startConfig.want_config_id = MY_CONFIG_ID  # we don't use this value
        self._sendToRadio(startConfig)

    def _sendToRadio(self, toRadio):
        """Send a ToRadio protobuf to the device"""
        logging.error(f"Subclass must provide toradio: {toRadio}")

    def _handleFromRadio(self, fromRadioBytes):
        """
        Handle a packet that arrived from the radio(update model and publish events)

        Called by subclasses."""
        fromRadio = mesh_pb2.FromRadio()
        fromRadio.ParseFromString(fromRadioBytes)
        json = google.protobuf.json_format.MessageToJson(fromRadio)
        logging.debug(f"Received: {json}")
        if fromRadio.HasField("my_info"):
            self.myInfo = fromRadio.my_info
        elif fromRadio.HasField("radio"):
            self.radioConfig = fromRadio.radio
        elif fromRadio.HasField("node_info"):
            node = fromRadio.node_info
            self._nodesByNum[node.num] = node
            self.nodes[node.user.id] = node
        elif fromRadio.config_complete_id == MY_CONFIG_ID:
            # we ignore the config_complete_id, it is unneeded for our stream API fromRadio.config_complete_id
            self._connected()
        elif fromRadio.HasField("packet"):
            self._handlePacketFromRadio(fromRadio.packet)
        elif fromRadio.rebooted:
            self._disconnected()
            self._startConfig()  # redownload the node db etc...
        else:
            logging.warn("Unexpected FromRadio payload")

    def _nodeNumToId(self, num):
        if num == BROADCAST_NUM:
            return BROADCAST_ADDR

        try:
            return self._nodesByNum[num].user.id
        except:
            logging.error("Node not found for fromId")
            return None

    def _handlePacketFromRadio(self, meshPacket):
        """Handle a MeshPacket that just arrived from the radio

        Will publish one of the following events:
        - meshtastic.receive.position(packet = MeshPacket dictionary)
        - meshtastic.receive.user(packet = MeshPacket dictionary)
        - meshtastic.receive.data(packet = MeshPacket dictionary)
        """

        asDict = google.protobuf.json_format.MessageToDict(meshPacket)
        # /add fromId and toId fields based on the node ID
        asDict["fromId"] = self._nodeNumToId(asDict["from"])
        asDict["toId"] = self._nodeNumToId(asDict["to"])

        # We could provide our objects as DotMaps - which work with . notation or as dictionaries
        #asObj = DotMap(asDict)
        topic = None
        if meshPacket.payload.HasField("position"):
            topic = "meshtastic.receive.position"
            # FIXME, update node DB as needed
        if meshPacket.payload.HasField("user"):
            topic = "meshtastic.receive.user"
            # FIXME, update node DB as needed
        if meshPacket.payload.HasField("data"):
            topic = "meshtastic.receive.data"
            # For text messages, we go ahead and decode the text to ascii for our users
            # if asObj.payload.data.typ == "CLEAR_TEXT":
            #    asObj.payload.data.text = asObj.payload.data.payload.decode(
            #        "utf-8")

        pub.sendMessage(topic, packet=asDict, interface=self)


class StreamInterface(MeshInterface):
    """Interface class for meshtastic devices over a stream link (serial, TCP, etc)"""

    def __init__(self, devPath=None, debugOut=None):
        """Constructor, opens a connection to a specified serial port, or if unspecified try to 
        find one Meshtastic device by probing

        Keyword Arguments:
            devPath {string} -- A filepath to a device, i.e. /dev/ttyUSB0 (default: {None})
            debugOut {stream} -- If a stream is provided, any debug serial output from the device will be emitted to that stream. (default: {None})

        Raises:
            Exception: [description]
            Exception: [description]
        """

        if devPath is None:
            ports = util.findPorts()
            if len(ports) == 0:
                raise Exception("No Meshtastic devices detected")
            elif len(ports) > 1:
                raise Exception(
                    f"Multiple ports detected, you must specify a device, such as {ports[0].device}")
            else:
                devPath = ports[0]

        logging.debug(f"Connecting to {devPath}")
        self.devPath = devPath
        self._rxBuf = bytes()  # empty
        self._wantExit = False
        self.stream = serial.Serial(
            devPath, 921600, exclusive=True, timeout=0.5)
        self._rxThread = threading.Thread(target=self.__reader, args=())
        self._rxThread.start()
        MeshInterface.__init__(self, debugOut=debugOut)

    def _sendToRadio(self, toRadio):
        """Send a ToRadio protobuf to the device"""
        logging.debug(f"Sending: {toRadio}")
        b = toRadio.SerializeToString()
        bufLen = len(b)
        header = bytes([START1, START2, (bufLen >> 8) & 0xff,  bufLen & 0xff])
        self.stream.write(header)
        self.stream.write(b)
        self.stream.flush()

    def close(self):
        """Close a connection to the device"""
        logging.debug("Closing serial stream")
        # pyserial cancel_read doesn't seem to work, therefore we ask the reader thread to close things for us
        self._wantExit = True
        if self._rxThread != threading.current_thread():
            self._rxThread.join()  # wait for it to exit

    def __reader(self):
        """The reader thread that reads bytes from our stream"""
        empty = bytes()

        while not self._wantExit:
            b = self.stream.read(1)
            if len(b) > 0:
                # logging.debug(f"read returned {b}")
                c = b[0]
                ptr = len(self._rxBuf)

                # Assume we want to append this byte, fixme use bytearray instead
                self._rxBuf = self._rxBuf + b

                if ptr == 0:  # looking for START1
                    if c != START1:
                        self._rxBuf = empty  # failed to find start
                        if self.debugOut != None:
                            try:
                                self.debugOut.write(b.decode("utf-8"))
                            except:
                                self.debugOut.write('?')

                elif ptr == 1:  # looking for START2
                    if c != START2:
                        self.rfBuf = empty  # failed to find start2
                elif ptr >= HEADER_LEN:  # we've at least got a header
                    # big endian length follos header
                    packetlen = (self._rxBuf[2] << 8) + self._rxBuf[3]

                    if ptr == HEADER_LEN:  # we _just_ finished reading the header, validate length
                        if packetlen > MAX_TO_FROM_RADIO_SIZE:
                            self.rfBuf = empty  # length ws out out bounds, restart

                    if len(self._rxBuf) != 0 and ptr + 1 == packetlen + HEADER_LEN:
                        try:
                            self._handleFromRadio(self._rxBuf[HEADER_LEN:])
                        except Exception as ex:
                            logging.error(
                                f"Error handling FromRadio, possibly corrupted? {ex}")
                            traceback.print_exc()
                        self._rxBuf = empty
            else:
                # logging.debug(f"timeout on {self.devPath}")
                pass
        logging.debug("reader is exiting")
        self.stream.close()
        self._disconnected()