#!/usr/bin/env python
"""
Processes mysql slow query log data and notifies Rollbar of slow queries.
"""

import optparse
import re
import sys

import rollbar

VERSION = 0.1

TIME_PATTERN = r'^#\s+Time:\s+(?P<date>[0-9]{6})\s+(?P<time>[0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2})$'
USER_HOST_PATTERN = r'^# User@Host: (?P<user_host>.* @ .*)$'
QUERY_STATS_PATTERN = r'^# Query_time: (?P<query_seconds>[0-9]+\.[0-9]+)\s+' \
                      r'Lock_time: (?P<lock_time>[0-9]+\.[0-9]+)\s+' \
                      r'Rows_sent: (?P<rows_sent>[0-9]+)\s+' \
                      r'Rows_examined: (?P<rows_examined>[0-9]+)$'

QUERY_PATTERN = r'^\s*(?P<query>[^;]+;)$'

# replication adds in the SET timestamp= line which we'll just ignore
IGNORE_PATTERNS = (r'^\s*use .*;$', r'^\s*SET timestamp=[0-9]+;$')

# e.g.
#
# # Time: 121228 15:24:25
# # User@Host: user[db] @ host [10.10.10.10]
# # Query_time: 0.000255  Lock_time: 0.000044 Rows_sent: 590  Rows_examined: 590
# SET timestamp=1356737065;
# SELECT foo FROM bar
# WHERE x = 2;
#
HEADER_REGEX = re.compile(TIME_PATTERN + '\n' + USER_HOST_PATTERN + '\n' + QUERY_STATS_PATTERN, flags=re.MULTILINE)
QUERY_REGEX = re.compile(QUERY_PATTERN, flags=re.MULTILINE | re.IGNORECASE | re.UNICODE)
IGNORE_REGEX = re.compile('|'.join(map(lambda x: '(%s)' % x, IGNORE_PATTERNS)), flags=re.MULTILINE)

NOTIFICATION_LEVELS = {'debug': 0, 'info': 1, 'warning': 2, 'error': 3, 'critical': 4}

heuristics = None
notification_level = 'warning'


def process_event(header, event):
    """
    Notify Rollbar about this query if the event passes the heuristics.
    """
    for name, heuristic in heuristics.iteritems():
        level = heuristic(header, event)
        if level and NOTIFICATION_LEVELS[level] >= notification_level:
            extra = {'header': header, 'data': event}
            if debug:
                print("\n===== DEBUG REPORT =====\n")
                print("rollbar.report_message(%(name)s, level=%(level)s, extra_data=%(extra)r, "
                      "payload_data={'language': 'sql'})" %
                      {'name': name, 'level': level, 'extra': extra})
            else:
                rollbar.report_message(name, level=level, extra_data=extra, payload_data={'language': 'sql'})


def process_input():
    lines = ''
    current_header = None
    while True:
        line = sys.stdin.readline()
        if line:
            tmp = lines + line

            header = HEADER_REGEX.search(tmp)
            clear_lines = False
            if header:
                current_header = header.groupdict()
                clear_lines = True
            elif current_header:
                matched_queries = QUERY_REGEX.finditer(tmp)
                for match in matched_queries:
                    clear_lines = True
                    event = match.groupdict()
                    if not IGNORE_REGEX.match(event['query']):
                        process_event(current_header, event)

            if clear_lines:
                lines = ''
            else:
                lines = tmp
        else:
            break


def build_heuristics(opts):
    return {
        'Slow query': SlowQuery(0.00001, 0.0001, 0.001, 0.01, 0.1),
        'Too many rows returned': TooManyRowsReturned(100, 1000, 10000, 100000, 100000),
        'Too many rows examined': TooManyRowsExamined(100, 1000, 10000, 100000, 100000),
        'Ratio of examined to returned is too high': RatioOfExaminedRowsTooHigh(10, 100, 1000, 10000, 100000),
        'Long lock time': LongLockTime(0.00001, 0.0001, 0.001, 0.01, 0.1)
    }


def build_option_parser():
    usage = 'usage: %prog [options] access_token'
    parser = optparse.OptionParser(usage=usage, version='%%prog %f' % VERSION)

    parser.add_option('-e',
                      '--environment',
                      dest='environment',
                      action='store',
                      type='string',
                      default='production',
                      help='The environment in which the mysql instance is running.')

    parser.add_option('-l',
                      '--level',
                      dest='notification_level',
                      type='int',
                      default=NOTIFICATION_LEVELS['warning'],
                      help='The minimum level to notify Rollbar at. ' \
                           'Valid values: 0 - debug, 1 - info, 2 - warning, 3 - error, ' \
                           '4 - critical')

    parser.add_option('-D',
                      '--debug',
                      dest='debug',
                      action='store_true',
                      default=False,
                      help='Run in debug mode; does not report to rollbar; prints to stdout.')
    return parser


def main():
    global heuristics, notification_level, debug

    parser = build_option_parser()
    (options, args) = parser.parse_args(sys.argv)

    if len(args) != 2:
        parser.error('incorrect number of arguments')
        sys.exit(1)

    access_token = args[1]
    environment = options.environment
    notification_level = min(NOTIFICATION_LEVELS['critical'],
                             max(NOTIFICATION_LEVELS['debug'],
                                 options.notification_level))
    debug = options.debug

    rollbar.init(access_token, environment)

    heuristics = build_heuristics(options)

    return process_input()


## Heuristics

class Heuristic(object):
    def __init__(self, min_val, max_debug_val, max_info_val, max_warning_val, max_error_val):
        self.ranges = list(reversed([('debug', min_val, max_debug_val),
                                     ('info', max_debug_val, max_info_val),
                                     ('warning', max_info_val, max_warning_val),
                                     ('error', max_warning_val, max_error_val),
                                     ('critical', max_error_val, None)]))

    def __call__(self, header, val):
        return self.check(self.calculate_val(header, val))

    def check(self, val):
        for name, min, max in self.ranges:
            if val >= min and (val < max if max is not None else True):
                return name

        return None

    def calculate_val(self, header, event):
        raise NotImplementedError()


class SlowQuery(Heuristic):
    def calculate_val(self, header, event):
        return float(header['query_seconds'])


class TooManyRowsReturned(Heuristic):
    def calculate_val(self, header, event):
        return int(header['rows_sent'])


class TooManyRowsExamined(Heuristic):
    def calculate_val(self, header, event):
        return int(header['rows_examined'])


class RatioOfExaminedRowsTooHigh(Heuristic):
    def calculate_val(self, header, event):
        if int(header['rows_sent']) > 0:
            return int(header['rows_examined']) / float(header['rows_sent'])
        else:
            return 0


class LongLockTime(Heuristic):
    def calculate_val(self, header, event):
        return float(header['lock_time'])


## Main

if __name__ == '__main__':
    main()
