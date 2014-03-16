#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# crawl.py - Greenlets-based Bitcoin network crawler.
#
# Copyright (c) 2014 Addy Yeow Chin Heng <ayeowch@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Greenlets-based Bitcoin network crawler.
"""

from gevent import monkey
monkey.patch_all()

import gevent
import json
import logging
import os
import redis
import redis.connection
import requests
import socket
import sys
import time
from ConfigParser import ConfigParser

from protocol import ProtocolError, Connection, DEFAULT_PORT

redis.connection.socket = gevent.socket

# Possible fields for a hash in Redis
TAG_FIELD = 'T'
DATA_FIELD = 'D'  # __START_HEIGHT__

# Possible values for a tag field in Redis
GREEN = 'G'  # Reachable node

# Redis connection setup
REDIS_HOST = os.environ.get('REDIS_HOST', "localhost")
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
REDIS_CONN = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT,
                               password=REDIS_PASSWORD)

SETTINGS = {}


def enumerate_node(redis_pipe, key, version_msg, addr_msg):
    """
    Stores start height for a reachable node.
    Adds all peering nodes with max. age of 24 hours into the crawl set.
    """
    redis_pipe.hset(key, DATA_FIELD, version_msg.get('start_height', 0))

    if 'addr_list' in addr_msg:
        now = time.time()

        for peer in addr_msg['addr_list']:
            age = now - peer['timestamp']  # seconds

            # Add peering node with age <= 24 hours into crawl set
            # Ignore non default port peer
            if age >= 0 and age <= SETTINGS['max_age'] and peer['port'] == DEFAULT_PORT:
                address = peer['ipv4'] if peer['ipv4'] else peer['ipv6']
                port = peer['port'] if peer['port'] > 0 else DEFAULT_PORT
                redis_pipe.sadd('pending', (address, port))


def connect(redis_conn, key):
    """
    Establishes connection with a node to:
    1) Send version message
    2) Receive version and verack message
    3) Send getaddr message
    4) Receive addr message containing list of peering nodes
    Stores node in Redis.
    """
    handshake_msgs = []
    addr_msg = {}

    redis_conn.hset(key, TAG_FIELD, "")  # Set Redis hash for a new node

    (address, port) = key[5:].split("-", 1)
    start_height = int(redis_conn.get('start_height'))

    connection = Connection((address, int(port)),
                            socket_timeout=SETTINGS['socket_timeout'],
                            user_agent=SETTINGS['user_agent'],
                            start_height=start_height)
    try:
        connection.open()
        handshake_msgs = connection.handshake()
        addr_msg = connection.getaddr()
    except ProtocolError as err:
        logging.debug("{}".format(err))
    except socket.error as err:
        logging.debug("{}".format(err))
    finally:
        connection.close()

    redis_pipe = redis_conn.pipeline()
    if len(handshake_msgs) > 0:
        enumerate_node(redis_pipe, key, handshake_msgs[0], addr_msg)
        redis_pipe.hset(key, TAG_FIELD, GREEN)
    redis_pipe.execute()


def dump(nodes):
    """
    Dumps data for reachable nodes into timestamp-prefixed JSON file.
    """
    json_data = []

    logging.info("Reachable nodes: {}".format(len(nodes)))
    for node in nodes:
        start_height = REDIS_CONN.hget(node, DATA_FIELD)
        (address, port) = node[5:].split("-", 1)
        json_data.append([address, int(port), int(start_height)])

    json_output = os.path.join(SETTINGS['crawl_dir'],
                               "{}.json".format(int(time.time())))
    open(json_output, 'w').write(json.dumps(json_data))
    logging.info("Wrote {}".format(json_output))


def restart():
    """
    Dumps data for the reachable nodes into a JSON file.
    Fetches latest start height.
    Loads all reachable nodes from Redis into the crawl set.
    Removes keys for all nodes from current crawl.
    """
    nodes = []  # Reachable nodes

    keys = REDIS_CONN.keys('node:*')
    logging.debug("Keys: {}".format(len(keys)))

    redis_pipe = REDIS_CONN.pipeline()
    for key in keys:
        tag = REDIS_CONN.hget(key, TAG_FIELD)
        if tag == GREEN:
            nodes.append(key)
            (address, port) = key[5:].split("-", 1)
            redis_pipe.sadd('pending', (address, int(port)))
        redis_pipe.delete(key)

    dump(nodes)

    set_start_height()

    redis_pipe.execute()


def cron():
    """
    Assigned to a worker to perform the following tasks periodically to
    maintain a continuous crawl:
    1) Reports the current number of nodes in crawl set
    2) Initiates a new crawl once the crawl set is empty
    """
    start = int(time.time())

    while True:
        pending_nodes = REDIS_CONN.scard('pending')
        logging.info("Pending: {}".format(pending_nodes))

        if pending_nodes == 0:
            elapsed = int(time.time()) - start
            REDIS_CONN.set('elapsed', elapsed)
            logging.info("Elapsed: {}".format(elapsed))

            logging.info("Restarting")
            restart()

            start = int(time.time())

        gevent.sleep(SETTINGS['cron_delay'])


def task():
    """
    Assigned to a worker to retrieve (pop) a node from the crawl set and
    attempt to establish connection with a new node.
    """
    redis_conn = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT,
                                   password=REDIS_PASSWORD)

    while True:
        node = redis_conn.spop('pending')  # Pop random node from set
        if node is None:
            gevent.sleep(1)
            continue

        node = eval(node)  # Convert string from Redis to tuple
        key = "node:{}-{}".format(node[0], node[1])

        # Skip IPv6 node
        if ":" in node[0] and not SETTINGS['ipv6']:
            continue

        if redis_conn.exists(key):
            continue

        connect(redis_conn, key)
        gevent.sleep(0)


def set_start_height():
    """
    Fetches current start height from a remote source. The value is then set
    in Redis for use by all workers.
    """
    try:
        start_height = int(requests.get(SETTINGS['height_url']).text)
    except requests.exceptions.RequestException as err:
        logging.warning("{}".format(err))
        start_height = int(REDIS_CONN.get('start_height'))
    logging.info("Start height: {}".format(start_height))
    REDIS_CONN.set('start_height', start_height)


def init_settings(argv):
    """
    Populates SETTINGS with key-value pairs from configuration file.
    """
    conf = ConfigParser()
    conf.read(argv[1])
    SETTINGS['logfile'] = conf.get('crawl', 'logfile')
    SETTINGS['seeds'] = conf.get('crawl', 'seeds')
    SETTINGS['height_url'] = conf.get('crawl', 'height_url')
    SETTINGS['workers'] = conf.getint('crawl', 'workers')
    SETTINGS['debug'] = conf.getboolean('crawl', 'debug')
    SETTINGS['user_agent'] = conf.get('crawl', 'user_agent')
    SETTINGS['socket_timeout'] = conf.getint('crawl', 'socket_timeout')
    SETTINGS['cron_delay'] = conf.getint('crawl', 'cron_delay')
    SETTINGS['max_age'] = conf.getint('crawl', 'max_age')
    SETTINGS['ipv6'] = conf.getboolean('crawl', 'ipv6')
    SETTINGS['crawl_dir'] = conf.get('crawl', 'crawl_dir')
    if not os.path.exists(SETTINGS['crawl_dir']):
        os.makedirs(SETTINGS['crawl_dir'])


def main(argv):
    if len(argv) < 2 or not os.path.exists(argv[1]):
        print("Usage: crawl.py [config]")
        return 1

    # Initialize global settings
    init_settings(argv)

    # Initialize logger
    loglevel = logging.INFO
    if SETTINGS['debug']:
        loglevel = logging.DEBUG

    logformat = ("%(asctime)s,%(msecs)05.1f %(levelname)s (%(funcName)s) "
                 "%(message)s")
    logging.basicConfig(level=loglevel,
                        format=logformat,
                        filename=SETTINGS['logfile'],
                        filemode='w')
    print("Writing output to {}, press CTRL+C to terminate..".format(
          SETTINGS['logfile']))

    logging.info("Removing all keys")
    keys = REDIS_CONN.keys('node:*')
    redis_pipe = REDIS_CONN.pipeline()
    for key in keys:
        redis_pipe.delete(key)
    redis_pipe.delete('pending')
    redis_pipe.execute()

    # Get seed nodes
    seeds = json.loads(open(SETTINGS['seeds'], 'r').read())
    for seed in seeds:
        REDIS_CONN.sadd('pending', (str(seed), DEFAULT_PORT))
    logging.info("Seeds: {}".format(len(seeds)))

    set_start_height()

    # Spawn workers (greenlets) including one worker reserved for cron tasks
    workers = []
    workers.append(gevent.spawn(cron))
    for _ in xrange(SETTINGS['workers'] - 1):
        workers.append(gevent.spawn(task))
    logging.info("Workers: {}".format(len(workers)))
    gevent.joinall(workers)

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
