import logging
import os
import Queue
import threading
import time

import protocol
import shared
import state
from bmconfigparser import BMConfigParser
from class_outgoingSynSender import outgoingSynSender
from class_sendDataThread import sendDataThread
from helper_sql import sqlStoredProcedure
from helper_threading import StoppableThread
from inventory import Inventory
from knownnodes import saveKnownNodes
from queues import (UISignalQueue, addressGeneratorQueue, objectProcessorQueue,
                    workerQueue)


logger = logging.getLogger(__name__)


def doCleanShutdown():
    state.shutdown = 1 #Used to tell proof of work worker threads and the objectProcessorThread to exit.

    #Stop sources of new threads
    for thread in threading.enumerate():
        if type(thread).__name__ not in ('outgoingSynSender', 'singleListener'):
            continue
        thread.stopThread()
        thread.join()

    protocol.broadcastToSendDataQueues((0, 'shutdown', 'no data'))   
    objectProcessorQueue.put(('checkShutdownVariable', 'no data'))
    for thread in threading.enumerate():
        if thread.isAlive() and isinstance(thread, StoppableThread):
            thread.stopThread()
    
    UISignalQueue.put(('updateStatusBar','Saving the knownNodes list of peers to disk...'))
    logger.info('Saving knownNodes list of peers to disk')
    saveKnownNodes()
    logger.info('Done saving knownNodes list of peers to disk')
    UISignalQueue.put(('updateStatusBar','Done saving the knownNodes list of peers to disk.'))
    logger.info('Flushing inventory in memory out to disk...')
    UISignalQueue.put((
        'updateStatusBar',
        'Flushing inventory in memory out to disk. This should normally only take a second...'))
    Inventory().flush()

    # Verify that the objectProcessor has finished exiting. It should have incremented the 
    # shutdown variable from 1 to 2. This must finish before we command the sqlThread to exit.
    for thread in threading.enumerate():
        if type(thread).__name__ != 'objectProcessor':
            continue
        thread.join()
        break
    
    # This will guarantee that the previous flush committed and that the
    # objectProcessorThread committed before we close the program.
    sqlStoredProcedure('commit')
    logger.info('Finished flushing inventory.')
    sqlStoredProcedure('exit')
    for thread in threading.enumerate():
        if type(thread).__name__ != 'sqlThread':
            continue
        thread.join()
        break
    
    # Wait long enough to guarantee that any running proof of work worker threads will check the
    # shutdown variable and exit. If the main thread closes before they do then they won't stop.
    time.sleep(.25)

    for thread in threading.enumerate():
        if isinstance(thread, sendDataThread):
            thread.sendDataThreadQueue.put((0, 'shutdown','no data'))
        if thread is not threading.currentThread() and isinstance(thread, StoppableThread) and not isinstance(thread, outgoingSynSender):
            logger.debug("Waiting for thread %s", thread.name)
            thread.join()

    # flush queued
    for queue in (workerQueue, UISignalQueue, addressGeneratorQueue, objectProcessorQueue):
        while True:
            try:
                queue.get(False)
                queue.task_done()
            except Queue.Empty:
                break

    logger.info('Clean shutdown complete.')

    for thread in threading.enumerate():
        if thread is threading.currentThread():
            continue
        logger.debug("Thread %s still running", thread.name)

    if BMConfigParser().safeGetBoolean('bitmessagesettings','daemon'):
        shared.thisapp.cleanup()
        os._exit(0)
