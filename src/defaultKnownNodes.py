import pickle
import time

import state


def createDefaultKnownNodes(appdata):
    ############## Stream 1 ################
    stream1 = {}

    #stream1[state.Peer('2604:2000:1380:9f:82e:148b:2746:d0c7', 8080)] = int(time.time())
    stream1[state.Peer('5.45.99.75', 8444)] = int(time.time())
    stream1[state.Peer('75.167.159.54', 8444)] = int(time.time())
    stream1[state.Peer('95.165.168.168', 8444)] = int(time.time())
    stream1[state.Peer('85.180.139.241', 8444)] = int(time.time())
    stream1[state.Peer('158.222.217.190', 8080)] = int(time.time())
    stream1[state.Peer('178.62.12.187', 8448)] = int(time.time())
    stream1[state.Peer('24.188.198.204', 8111)] = int(time.time())
    stream1[state.Peer('109.147.204.113', 1195)] = int(time.time())
    stream1[state.Peer('178.11.46.221', 8444)] = int(time.time())
    
    ############# Stream 2 #################
    stream2 = {}
    # None yet

    ############# Stream 3 #################
    stream3 = {}
    # None yet

    allKnownNodes = {}
    allKnownNodes[1] = stream1
    allKnownNodes[2] = stream2
    allKnownNodes[3] = stream3

    #print stream1
    #print allKnownNodes

    with open(appdata + 'knownnodes.dat', 'wb') as output:
        # Pickle dictionary using protocol 0.
        pickle.dump(allKnownNodes, output)

    return allKnownNodes
