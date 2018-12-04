import collections
from datetime import datetime
import intake.catalog
import intake.catalog.base
import intake.catalog.local
import intake.catalog.remote
import intake.source.base
import intake_xarray.base
import itertools
import msgpack
import numpy
import pandas
import pymongo
import pymongo.errors
import requests
from requests.compat import urljoin, urlparse
import time
import xarray


class FacilityCatalog(intake.catalog.Catalog):
    "spans multiple MongoDB instances"
    ...


class MongoMetadataStoreCatalog(intake.catalog.Catalog):
    "represents one MongoDB instance"
    name = 'mongo_metadatastore'

    def __init__(self, uri, *, query=None, **kwargs):
        """
        A Catalog backed by a MongoDB with metadatastore v1 collections.

        Parameters
        ----------
        uri : string
            This URI must include a database name.
            Example: ``mongodb://localhost:27107/database_name``.
        query : dict
            Mongo query. This is used internally by the ``search`` method.
        **kwargs :
            passed up to the Catalog base class
        """
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self._uri = uri
        self._query = query
        self._client = pymongo.MongoClient(uri)
        kwargs.setdefault('name', uri)
        try:
            # Called with no args, get_database() returns the database
            # specified in the uri --- or raises if there was none. There is no
            # public method for checking this in advance, so we just catch the
            # error.
            db = self._client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                "Invalid uri. Did you forget to include a database?") from err
        self._run_start_collection = db.get_collection('run_start')
        self._run_stop_collection = db.get_collection('run_stop')
        self._event_descriptor_collection = db.get_collection('event_descriptor')
        self._event_collection = db.get_collection('event')
        self._query = query or {}
        super().__init__(**kwargs)
        if self.metadata is None:
            self.metadata = {}

        catalog = self

        class Entries:
            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):
                args = dict(
                    run_start_doc=run_start_doc,
                    run_stop_collection=catalog._run_stop_collection,
                    event_descriptor_collection=catalog._event_descriptor_collection,
                    event_collection=catalog._event_collection)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.RunCatalog',
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters={},
                    metadata={},  # TODO
                    catalog_dir=None,
                    getenv=True,
                    getshell=True,
                    catalog=catalog)

            def __iter__(self):
                yield from self.keys()

            def keys(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    yield run_start_doc['uid']

            def values(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield self._doc_to_entry(run_start_doc)

            def items(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield run_start_doc['uid'], self._doc_to_entry(run_start_doc)

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    name = int(name)
                except ValueError:
                    pass
                if isinstance(name, int):
                    if name < 0:
                        # Interpret negative N as "the Nth from last entry".
                        query = catalog._query
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING) .limit(name))
                        *_, run_start_doc = cursor
                    else:
                        # Interpret positive N as
                        # "most recent entry with scan_id == N".
                        query = {'$and': [catalog._query, {'scan_id': name}]}
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING)
                                .limit(1))
                        run_start_doc, = cursor
                else:
                    query = {'$and': [catalog._query, {'uid': name}]}
                    run_start_doc = catalog._run_start_collection.find_one(query)
                if run_start_doc is None:
                    raise KeyError(name)
                del run_start_doc['_id']  # Drop internal Mongo detail.
                return self._doc_to_entry(run_start_doc)

        self._entries = Entries()
        self._schema = {}  # TODO This is cheating, I think.

    def _close(self):
        self._client.close()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
            MongoDB query.
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = MongoMetadataStoreCatalog(
            uri=self._uri,
            query=query,
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        cat.metadata['search'] = {'query': query, 'upstream': self.name}
        return cat


class RunCatalog(intake.catalog.Catalog):
    "represents one Run"
    container = 'catalog'
    name = 'run'
    version = '0.0.1'
    partition_access = True

    def __init__(self,
                 run_start_doc,
                 run_stop_collection,
                 event_descriptor_collection,
                 event_collection,
                 **kwargs):
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self.urlpath = ''  # TODO Not sure why I had to add this.

        self._run_start_doc = run_start_doc
        self._run_stop_doc = None  # loaded in _load below
        self._run_stop_collection = run_stop_collection
        self._event_descriptor_collection = event_descriptor_collection
        self._event_collection = event_collection

        super().__init__(**kwargs)
        self._schema = {}  # TODO This is cheating, I think.

    def __repr__(self):
        try:
            start = self._run_start_doc
            stop = self._run_stop_doc or {}
            out = (f"<Intake catalog: Run {start['uid'][:8]}...>\n"
                   f"  {_ft(start['time'])} -- {_ft(stop.get('time', '?'))}\n")
                   # f"  Streams:\n")
            # for stream_name in self:
            #     out += f"    * {stream_name}\n"
        except Exception as exc:
            out = f"<Intake catalog: Run *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _load(self):
        uid = self._run_start_doc['uid']
        run_stop_doc = self._run_stop_collection.find_one({'run_start': uid})
        self._run_stop_doc = run_stop_doc
        if run_stop_doc is not None:
            del run_stop_doc['_id']  # Drop internal Mongo detail.
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': run_stop_doc})
        cursor = self._event_descriptor_collection.find(
            {'run_start': uid},
            sort=[('time', pymongo.ASCENDING)])
        # Sort descriptors like
        # {'stream_name': [descriptor1, descriptor2, ...], ...}
        streams = itertools.groupby(cursor,
                                    key=lambda d: d.get('name'))

        # Make a MongoEventStreamSource for each stream_name.
        for stream_name, event_descriptor_docs in streams:
            args = {'run_start_doc': self._run_start_doc,
                    'event_descriptor_docs': list(event_descriptor_docs),
                    'event_collection': self._event_collection,
                    'run_stop_collection': self._run_stop_collection}
            self._entries[stream_name] = intake.catalog.local.LocalCatalogEntry(
                name=stream_name,
                description={},  # TODO
                driver='intake_bluesky.MongoEventStream',
                direct_access='forbid',  # ???
                args=args,
                cache=None,  # ???
                parameters={},
                metadata={},  # TODO
                catalog_dir=None,
                getenv=True,
                getshell=True,
                catalog=self)


class MongoEventStream(intake_xarray.base.DataSourceMixin):
    container = 'xarray'
    name = 'event-stream'
    version = '0.0.1'
    partition_access = True

    def __init__(self, run_start_doc, event_descriptor_docs, event_collection,
                 run_stop_collection, metadata=None):
        # self._partition_size = 10
        # self._default_chunks = 10
        self.urlpath = ''  # TODO Not sure why I had to add this.
        self._run_start_doc = run_start_doc
        self._run_stop_doc  = None
        self._event_collection = event_collection
        self._event_descriptor_docs = event_descriptor_docs
        self._stream_name = event_descriptor_docs[0].get('name')
        self._run_stop_collection = run_stop_collection
        self._ds = None  # set by _open_dataset below
        super().__init__(
            metadata=metadata
        )
        if self.metadata is None:
            self.metadata = {}

    def __repr__(self):
        try:
            out = (f"<Intake catalog: Stream {self._stream_name!r} "
                   f"from Run {self._run_start_doc['uid'][:8]}...>")
        except Exception as exc:
            out = f"<Intake catalog: Stream *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _open_dataset(self):
        uid = self._run_start_doc['uid']
        run_stop_doc = self._run_stop_collection.find_one({'run_start': uid})
        self._run_stop_doc = run_stop_doc
        if run_stop_doc is not None:
            del run_stop_doc['_id']  # Drop internal Mongo detail.
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': run_stop_doc})
        # TODO pass this in from BlueskyEntry
        slice_ = slice(None)
        include = []
        exclude = []

        if isinstance(slice_, collections.Iterable):
            first_axis = slice_[0]
        else:
            first_axis = slice_
        query = {'descriptor': {'$in': [d['uid']
                                for d in self._event_descriptor_docs]}}
        seq_num_filter = {}
        if first_axis.start is not None:
            seq_num_filter['$gte'] = first_axis.start
        if first_axis.stop is not None:
            seq_num_filter['$lt'] = first_axis.stop
        if first_axis.step is not None:
            # Have to think about how this interacts with repeated seq_num.
            raise NotImplementedError
        if seq_num_filter:
            query['seq_num'] = seq_num_filter
        cursor = self._event_collection.find(
            query,
            sort=[('descriptor', pymongo.DESCENDING),
                  ('time', pymongo.ASCENDING)])

        events = list(cursor)
        # Put time into a vectorized data sturcutre with suitable dimensions
        # for xarray coords.
        times = numpy.expand_dims([ev['time'] for ev in events], 0)
        # seq_nums = numpy.expand_dims([ev['seq_num'] for ev in events], 0)
        # uids = [ev['uid'] for ev in events]
        data_keys = self._event_descriptor_docs[0]['data_keys']
        if include:
            keys = list(set(data_keys) & set(include))
        elif exclude:
            keys = list(set(data_keys) - set(exclude))
        else:
            keys = list(data_keys)
        data_table = _transpose(events, keys, 'data')
        # timestamps_table = _transpose(all_events, keys, 'timestamps')
        # data_keys = descriptor['data_keys']
        # external_keys = [k for k in data_keys if 'external' in data_keys[k]]
        data_arrays = {}
        for key in keys:
            if data_keys[key].get('external'):
                raise NotImplementedError
            else:
                data_arrays[key] = xarray.DataArray(data=data_table[key],
                                                    dims=('time',),
                                                    coords=times,
                                                    name=key)
        self._ds = xarray.Dataset(data_vars=data_arrays)


def _transpose(in_data, keys, field):
    """Turn a list of dicts into dict of lists

    Parameters
    ----------
    in_data : list
        A list of dicts which contain at least one dict.
        All of the inner dicts must have at least the keys
        in `keys`

    keys : list
        The list of keys to extract

    field : str
        The field in the outer dict to use

    Returns
    -------
    transpose : dict
        The transpose of the data
    """
    out = {k: [None] * len(in_data) for k in keys}
    for j, ev in enumerate(in_data):
        dd = ev[field]
        for k in keys:
            out[k][j] = dd[k]
    return out

def _ft(timestamp):
    "format timestamp"
    if isinstance(timestamp, str):
        return timestamp
    # Truncate microseconds to miliseconds. Do not bother to round.
    return (datetime.fromtimestamp(timestamp)
            .strftime('%Y-%m-%d %H:%M:%S.%f'))[:-3]


class MongoInsertCallback:
    """
    This is a replacmenet for db.insert.
    """
    def __init__(self, uri):
        self._uri = uri
        self._client = pymongo.MongoClient(uri)
        try:
            # Called with no args, get_database() returns the database
            # specified in the uri --- or raises if there was none. There is no
            # public method for checking this in advance, so we just catch the
            # error.
            db = self._client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                "Invalid uri. Did you forget to include a database?") from err

        self._run_start_collection = db.get_collection('run_start')
        self._run_stop_collection = db.get_collection('run_stop')
        self._event_descriptor_collection = db.get_collection('event_descriptor')
        self._event_collection = db.get_collection('event')

    def __call__(self, name, doc):
        getattr(self, name)(doc)

    def start(self, doc):
        self._run_start_collection.insert_one(doc)

    def descriptor(self, doc):
        self._event_descriptor_collection.insert_one(doc)

    def event(self, doc):
        self._event_collection.insert_one(doc)

    def stop(self, doc):
        self._run_stop_collection.insert_one(doc)
