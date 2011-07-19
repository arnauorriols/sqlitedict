#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011 Radim Rehurek <radimrehurek@seznam.cz>

# Hacked together from:
#  * http://code.activestate.com/recipes/576638-draft-for-an-sqlite3-based-dbm/
#  * http://code.activestate.com/recipes/526618/
#
# Use the code in any way you like (at your own risk), it's public domain.

"""
A wrapper around sqlite3 database, with a dict-like interface:

>>> mydict = SqlDict('some.db', autocommit=True) # the mapping will be persisted to file some.db
>>> mydict['some_key'] = any_picklable_object
>>> print mydict['some_key']
>>> print len(mydict) # etc... all standard dict functions work

Pickle is used internally to serialize the values. Keys are strings.
If you don't use autocommit (default), don't forget to `mydict.commit()` when done
with a transaction.

Features:
* support for multiple dicts (SQLite tables) in the same database file
* support for multi-threaded access (needed by e.g. Pyro)

"""


import sqlite3
from cPickle import dumps, loads, HIGHEST_PROTOCOL as PICKLE_PROTOCOL
from UserDict import DictMixin
from Queue import Queue
from threading import Thread


def open(*args, **kwargs):
    """See documentation of the SqlDict class."""
    return SqlDict(*args, **kwargs)


def encode(obj):
    """Serialize an object using pickle to a binary format accepted by SQLite."""
    return sqlite3.Binary(dumps(obj, protocol=PICKLE_PROTOCOL))

def decode(obj):
    """Deserialize objects retrieved from SQLite."""
    return loads(str(obj))



class SqlDict(object, DictMixin):
    def __init__(self, filename=':memory:', tablename='shelf', flag='c', autocommit=False):
        """
        Initialize a thread-safe sqlite-backed dictionary. The dictionary will
        be a table `tablename` in database file `filename`. A single file (=database)
        may contain multiple tables.

        If you enable `autocommit`, changes will be committed after each operation
        (more inefficient but safer). Otherwise, changes are committed on `self.commit()`,
        `self.clear()` and `self.close()`.

        The `flag` parameter:
          'c': default mode, open for read/write, creating the db/table if necessary.
          'w': open for r/w, but drop `tablename` contents first (start with empty table)
          'n': create a new database (erasing any existing tables, not just `tablename`!).

        """
        if flag == 'n':
            import os
            if os.path.exists(filename):
                os.remove(filename)
        self.tablename = tablename

        MAKE_TABLE = 'CREATE TABLE IF NOT EXISTS %s (key TEXT PRIMARY KEY, value BLOB)' % self.tablename
        self.conn = SqliteMultithread(filename, autocommit=autocommit)
        self.conn.execute(MAKE_TABLE)
        self.conn.commit()
        if flag == 'w':
            self.clear()


    def __len__(self):
        GET_LEN = 'SELECT COUNT(*) FROM %s' % self.tablename
        return self.conn.select_one(GET_LEN)[0]

    def __bool__(self):
        GET_LEN = 'SELECT MAX(ROWID) FROM %s' % self.tablename
        return self.conn.select_one(GET_LEN) is not None

    def iterkeys(self):
        GET_KEYS = 'SELECT key FROM %s' % self.tablename
        for key in self.conn.select(GET_KEYS):
            yield key[0]

    def itervalues(self):
        GET_VALUES = 'SELECT value FROM %s' % self.tablename
        for value in self.conn.select(GET_VALUES):
            yield decode(value[0])

    def iteritems(self):
        GET_ITEMS = 'SELECT key, value FROM %s' % self.tablename
        for key, value in self.conn.select(GET_ITEMS):
            yield key, decode(value)

    def __contains__(self, key):
        HAS_ITEM = 'SELECT 1 FROM %s WHERE key = ?' % self.tablename
        return self.conn.select_one(HAS_ITEM, (key,)) is not None

    def __getitem__(self, key):
        GET_ITEM = 'SELECT value FROM %s WHERE key = ?' % self.tablename
        item = self.conn.select_one(GET_ITEM, (key,))
        if item is None:
            raise KeyError(key)

        return decode(item[0])

    def __setitem__(self, key, value):
        ADD_ITEM = 'REPLACE INTO %s (key, value) VALUES (?,?)' % self.tablename
        self.conn.execute(ADD_ITEM, (key, encode(value)))

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        DEL_ITEM = 'DELETE FROM %s WHERE key = ?' % self.tablename
        self.conn.execute(DEL_ITEM, (key,))

    def update(self, items=(), **kwds):
        try:
            items = [(k, encode(v)) for k, v in items.iteritems()]
        except AttributeError:
            pass

        UPDATE_ITEMS = 'REPLACE INTO %s (key, value) VALUES (?, ?)' % self.tablename
        self.conn.executemany(UPDATE_ITEMS, items)
        if kwds:
            self.update(kwds)

    def keys(self):
        return list(self.iterkeys())

    def values(self):
        return list(self.itervalues())

    def items(self):
        return list(self.iteritems())

    def __iter__(self):
        return self.iterkeys()

    def clear(self):
        CLEAR_ALL = 'DELETE FROM %s;' % self.tablename # avoid VACUUM, as it gives "OperationalError: database schema has changed"
        self.conn.commit()
        self.conn.execute(CLEAR_ALL)
        self.conn.commit()

    def commit(self):
        if self.conn is not None:
            self.conn.commit()

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def __del__(self):
        """Make a best effort to commit any outstanding changes. Note that having
        `__del__` still doesn't ensure it will be called."""
        self.close()
#endclass SqlDict



class SqliteMultithread(Thread):
    """
    Wrap sqlite connection in a way that allows concurrent requests from multiple threads.

    This is done by internally queueing the requests and processing them sequentially
    in a separate thread (in the same order they arrived).

    """
    def __init__(self, filename, autocommit):
        super(SqliteMultithread, self).__init__()
        self.filename = filename
        self.autocommit = autocommit
        self.reqs = Queue() # use request queue of unlimited size
        self.setDaemon(True) # python2.5-compatible
        self.start()

    def run(self):
        conn = sqlite3.connect(self.filename, check_same_thread=False)
        conn.text_factory = str
        cursor = conn.cursor()
        while True:
            req, arg, res = self.reqs.get()
            if req=='--close--':
                break
            elif req=='--commit--':
                conn.commit()
            else:
                cursor.execute(req, arg)
                if res:
                    for rec in cursor:
                        res.put(rec)
                    res.put('--no more--')
                if self.autocommit:
                    conn.commit()
        conn.close()

    def execute(self, req, arg=None, res=None):
        """
        `execute` calls are non-blocking: just queue up the request and return immediately.

        """
        self.reqs.put((req, arg or tuple(), res))

    def executemany(self, req, items):
        for item in items:
            self.execute(req, item)

    def select(self, req, arg=None):
        """
        Unlike sqlite's native select, this select doesn't handle iteration efficiently.

        The result of `select` starts filling up with values as soon as the
        request is dequeued, and although you can iterate over the result normally
        (`for res in self.select(): ...`), the entire result will be in memory.

        """
        res = Queue() # results of the select will appear as items in this queue
        self.execute(req, arg, res)
        while True:
            rec = res.get()
            if rec == '--no more--':
                break
            yield rec

    def select_one(self, req, arg=None):
        """Return only the first row of the SELECT, or None if there are no matching rows."""
        try:
            return iter(self.select(req, arg)).next()
        except StopIteration:
            return None

    def commit(self):
        self.execute('--commit--')

    def close(self):
        self.execute('--close--')
#endclass SqliteMultithread


# running sqldict.py as script will perform a simple unit test
if __name__ in '__main___':
    for d in SqlDict(), SqlDict('example', flag='n'):
        assert list(d) == []
        assert len(d) == 0
        assert not d
        d['abc'] = 'rsvp' * 100
        assert d['abc'] == 'rsvp' * 100
        assert len(d) == 1
        d['abc'] = 'lmno'
        assert d['abc'] == 'lmno'
        assert len(d) == 1
        del d['abc']
        assert not d
        assert len(d) == 0
        d['abc'] = 'lmno'
        d['xyz'] = 'pdq'
        assert len(d) == 2
        assert list(d.iteritems()) == [('abc', 'lmno'), ('xyz', 'pdq')]
        assert d.items() == [('abc', 'lmno'), ('xyz', 'pdq')]
        assert d.values() == ['lmno', 'pdq']
        assert d.keys() == ['abc', 'xyz']
        assert list(d) == ['abc', 'xyz']
        d.update(p='x', q='y', r='z')
        assert len(d) == 5
        assert d.items() == [('abc', 'lmno'), ('xyz', 'pdq'), ('q', 'y'), ('p', 'x'), ('r', 'z')]
        del d['abc']
        try:
            error = d['abc']
        except KeyError:
            pass
        else:
            assert False
        try:
            del d['abc']
        except KeyError:
            pass
        else:
            assert False
        assert list(d) == ['xyz', 'q', 'p', 'r']
        assert d
        d.clear()
        assert not d
        assert list(d) == []
        d.update(p='x', q='y', r='z')
        assert list(d) == ['q', 'p', 'r']
        d.clear()
        assert not d
        d.close()
    print 'all tests passed :-)'

