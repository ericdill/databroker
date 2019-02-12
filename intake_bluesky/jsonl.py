import event_model
from functools import partial
import intake
import intake.catalog
import intake.catalog.local
import intake.source.base
import json
from mongoquery import Query

from .core import parse_handler_registry


class BlueskyJSONLCatalog(intake.catalog.Catalog):


    def __init__(self,  jsonl_filelist, *,
        handler_registry=None, query=None, **kwargs):

        """
        This Catalog is backed by a jsonl file.
        This Catalog uses a jsonl file, bluesky documents are store in
        chronological order in the jsonl file.

        Parameters
        ----------
        handler_registry : dict, optional
            Maps each asset spec to a handler class or a string specifying the
            module name and class name, as in (for example)
            ``{'SOME_SPEC': 'module.submodule.class_name'}``.
        **kwargs :
            Additional keyword arguments are passed through to the base class,
            Catalog.
        """

        self._update_index(jsonl_filelist)
        self._runs = {} # this maps run_start_uids to file paths
        self._run_starts = {} # this maps run_start_uids to run_start_docs


        self._query = query or {}
        if handler_registry is None:
            handler_registry = {}
        parsed_handler_registry = parse_handler_registry(handler_registry)
        self.filler = event_model.Filler(parsed_handler_registry)
        super().__init__(**kwargs)

    def _update_index(self, file_list):
        for run_file in file_list:
            with open(run_file, 'r') as f:
                name, run_start_doc = json.loads(f.readline())

                if name != 'start':
                    raise ValueError(f"Invalid file {run_file}: "
                                f"first line must be a valid start document.")

                if Query(self._query).match(run_start_doc):
                    run_start_uid = run_start['uid']
                    self._runs[run_start_uid] = run_file
                    self._run_starts[run_start_uid] = run_start_doc


    def _get_run_stop(self, run_start_uid):
        return doc

    def _get_event_descriptors(self, run_start_uid):
        return doc

    def _get_event_cursor(self, descriptor_uids, skip=0, limit=None):
        return doc

    def _get_event_count(self, descriptor_uids):
        return doc

    def _get_resource(self, uid):
        return doc

    def _get_datum(self, datum_id):
        return doc

    def _get_datum_cursor(self, resource_uid):
        return doc

    def _make_entries_container(self):
        catalog = self

        class Entries:
            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):
                uid = run_start_doc['uid']
                run_start_doc.pop('_id')
                entry_metadata = {'start': run_start_doc,
                                  'stop': catalog._get_run_stop(uid)}
                args = dict(
                    run_start_doc=run_start_doc,
                    get_run_stop=partial(catalog._get_run_stop, uid),
                    get_event_descriptors=partial(catalog._get_event_descriptors, uid),
                    get_event_cursor=catalog._get_event_cursor,
                    get_event_count=catalog._get_event_count,
                    get_resource=catalog._get_resource,
                    get_datum=catalog._get_datum,
                    get_datum_cursor=catalog._get_datum_cursor,
                    filler=catalog.filler)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.core.RunCatalog',
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters=[],
                    metadata=entry_metadata,
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
                    yield self._doc_to_entry(run_start_doc)

            def items(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    yield run_start_doc['uid'], self._doc_to_entry(run_start_doc)

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    name = int(name)
                except ValueError:
                    pass
                if isinstance(name, int):
                     raise NotImplementedError
                else:
                    run_start_doc = self._runs[name]
                    return self._doc_to_entry(run_start_doc)

            def __contains__(self, key):
                # Avoid iterating through all entries.
                try:
                    self[key]
                except KeyError:
                    return False
                else:
                    return True

        return Entries()

    def _close(self):
        self._jsonl_documents.close()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
        """
        return cat


intake.registry['mongo_metadatastore'] = BlueskyMongoCatalog
