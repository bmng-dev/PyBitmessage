# -*- coding: utf-8 -*-
'''
Levels:
    DEBUG       Detailed information, typically of interest only when diagnosing problems.
    INFO        Confirmation that things are working as expected.
    WARNING     An indication that something unexpected happened, or indicative of some problem in the
                near future (e.g. ‘disk space low’). The software is still working as expected.
    ERROR       Due to a more serious problem, the software has not been able to perform some function.
    CRITICAL    A serious error, indicating that the program itself may be unable to continue running.
'''
import logging
import logging.config
import os
import sys

import state


log_level = 'WARNING'

def log_uncaught_exceptions(ex_cls, ex, tb):
    logging.getLogger(__name__).error('Unhandled exception', exc_info=(ex_cls, ex, tb))

def configure_logging():
    have_logging = False
    try:
        logging.config.fileConfig(os.path.join (state.appdata, 'logging.dat'))
        have_logging = True
        print "Loaded logger configuration from %s" % (os.path.join(state.appdata, 'logging.dat'))
    except:
        if os.path.isfile(os.path.join(state.appdata, 'logging.dat')):
            print "Failed to load logger configuration from %s, using default logging config" % (os.path.join(state.appdata, 'logging.dat'))
            print sys.exc_info()
        else:
            # no need to confuse the user if the logger config is missing entirely
            print "Using default logger configuration"
    
    sys.excepthook = log_uncaught_exceptions

    if have_logging:
        return

    logging.config.dictConfig({
        'version': 1,
        'formatters': {
            'default': {
                'format': '%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - %(threadName)s - %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S',
            },
        },
        'handlers': {
            'file': {
                'class': 'logging.handlers.RotatingFileHandler',
                'formatter': 'default',
                'level': log_level,
                'filename': state.appdata + 'debug.log',
                'maxBytes': 2097152, # 2 MiB
                'backupCount': 1,
                'encoding': 'UTF-8',
            },
        },
        'root': {
            'level': 'NOTSET',
            'handlers': ['file'],
        },
    })
