# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

import hashlib
import itertools
import json
import pathlib
import re
import base64
import os
from collections import defaultdict
from errno import EACCES, EPERM, EROFS
from functools import partial
from io import open as io_open
from logging import getLogger
from os.path import dirname, exists, isdir, join, splitext
from time import time

from genericpath import getmtime, isfile

try:
    from tlz.itertoolz import groupby
except ImportError:
    from conda._vendor.toolz.itertoolz import groupby

from conda.core.repo import Response304ContentUnchanged

try:
    if os.environ.get("CONDA_NO_JLAP", False):
        raise ImportError("skip jlap")
    from conda.core.repo_jlap import CondaRepoJLAP as CondaRepoInterface
except ImportError:
    from conda.core.repo import CondaRepoInterface

from .. import CondaError
from .._vendor.boltons.setutils import IndexedSet
from ..auxlib.ish import dals
from ..base.constants import CONDA_PACKAGE_EXTENSION_V1, REPODATA_FN
from ..base.context import context
from ..common.compat import ensure_binary
from ..common.io import DummyExecutor, ThreadLimitedThreadPoolExecutor, dashlist
from ..common.path import url_to_path
from ..common.url import join_url, maybe_unquote
from ..core.package_cache_data import PackageCacheData
from ..exceptions import (
    CondaUpgradeError,
    NotWritableError,
)
from ..gateways.disk import mkdir_p, mkdir_p_sudo_safe
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.update import touch
from ..models.channel import Channel, all_channel_urls
from ..models.match_spec import MatchSpec
from ..models.records import PackageRecord
from ..trust.signature_verification import signature_verification

import pickle

log = getLogger(__name__)
stderrlog = getLogger('conda.stderrlog')

REPODATA_PICKLE_VERSION = 28
MAX_REPODATA_VERSION = 1
REPODATA_HEADER_RE = b'"(_etag|_mod|_cache_control)":[ ]?"(.*?[^\\\\])"[,}\\s]'  # NOQA


class SubdirDataType(type):

    def __call__(cls, channel, repodata_fn=REPODATA_FN):
        assert channel.subdir
        assert not channel.package_filename
        assert type(channel) is Channel
        now = time()
        repodata_fn = repodata_fn or REPODATA_FN
        cache_key = channel.url(with_credentials=True), repodata_fn
        if cache_key in SubdirData._cache_:
            cache_entry = SubdirData._cache_[cache_key]
            if cache_key[0].startswith('file://'):
                file_path = url_to_path(channel.url() + '/' + repodata_fn)
                if exists(file_path):
                    if cache_entry._mtime > getmtime(file_path):
                        return cache_entry
            else:
                return cache_entry
        subdir_data_instance = super(SubdirDataType, cls).__call__(channel, repodata_fn)
        subdir_data_instance._mtime = now
        SubdirData._cache_[cache_key] = subdir_data_instance
        return subdir_data_instance


class SubdirData(metaclass=SubdirDataType):
    _cache_ = {}

    @classmethod
    def clear_cached_local_channel_data(cls):
        # This should only ever be needed during unit tests, when
        # CONDA_USE_ONLY_TAR_BZ2 may change during process lifetime.
        cls._cache_ = {k: v for k, v in cls._cache_.items() if not k[0].startswith('file://')}

    @staticmethod
    def query_all(package_ref_or_match_spec, channels=None, subdirs=None,
                  repodata_fn=REPODATA_FN):
        from .index import check_allowlist  # TODO: fix in-line import

        # ensure that this is not called by threaded code
        create_cache_dir()
        if channels is None:
            channels = context.channels
        if subdirs is None:
            subdirs = context.subdirs
        channel_urls = all_channel_urls(channels, subdirs=subdirs)
        if context.offline:
            grouped_urls = groupby(lambda url: url.startswith('file://'), channel_urls)
            ignored_urls = grouped_urls.get(False, ())
            if ignored_urls:
                log.info("Ignoring the following channel urls because mode is offline.%s",
                         dashlist(ignored_urls))
            channel_urls = IndexedSet(grouped_urls.get(True, ()))
        check_allowlist(channel_urls)
        subdir_query = lambda url: tuple(SubdirData(Channel(url), repodata_fn=repodata_fn).query(
            package_ref_or_match_spec))

        # TODO test timing with ProcessPoolExecutor
        Executor = (DummyExecutor if context.debug or context.repodata_threads == 1
                    else partial(ThreadLimitedThreadPoolExecutor,
                                 max_workers=context.repodata_threads))
        with Executor() as executor:
            result = tuple(itertools.chain.from_iterable(executor.map(subdir_query, channel_urls)))
        return result

    def query(self, package_ref_or_match_spec):
        if not self._loaded:
            self.load()
        param = package_ref_or_match_spec
        if isinstance(param, str):
            param = MatchSpec(param)
        if isinstance(param, MatchSpec):
            if param.get_exact_value('name'):
                package_name = param.get_exact_value('name')
                for prec in self._names_index[package_name]:
                    if param.match(prec):
                        yield prec
            elif param.get_exact_value('track_features'):
                track_features = param.get_exact_value('track') or ()
                candidates = itertools.chain.from_iterable(self._track_features_index[feature_name]
                                    for feature_name in track_features)
                for prec in candidates:
                    if param.match(prec):
                        yield prec
            else:
                for prec in self._package_records:
                    if param.match(prec):
                        yield prec
        else:
            assert isinstance(param, PackageRecord)
            for prec in self._names_index[param.name]:
                if prec == param:
                    yield prec

    def __init__(self, channel, repodata_fn=REPODATA_FN):
        assert channel.subdir
        if channel.package_filename:
            parts = channel.dump()
            del parts['package_filename']
            channel = Channel(**parts)
        self.channel = channel
        # disallow None (typing)
        self.url_w_subdir = self.channel.url(with_credentials=False) or ""
        self.url_w_credentials = self.channel.url(with_credentials=True) or ""
        # whether or not to try using the new, trimmed-down repodata
        self.repodata_fn = repodata_fn
        self._loaded = False
        self._key_mgr = None

        self._repo = CondaRepoInterface(
            self.url_w_credentials,
            self.repodata_fn,
            cache_path_json=self.cache_path_json,
            cache_path_state=self.cache_path_state)

    def reload(self):
        self._loaded = False
        self.load()
        return self

    @property
    def cache_path_base(self):
        return join(
            create_cache_dir(),
            splitext(cache_fn_url(self.url_w_credentials, self.repodata_fn))[0])

    @property
    def url_w_repodata_fn(self):
        return self.url_w_subdir + '/' + self.repodata_fn

    @property
    def cache_path_json(self):
        return self.cache_path_base + ('1' if context.use_only_tar_bz2 else '') + '.json'

    @property
    def cache_path_state(self):
        """
        Out-of-band etag and other state needed by the RepoInterface.
        """
        return self.cache_path_base + ".state.json"

    @property
    def cache_path_pickle(self):
        return self.cache_path_base + ('1' if context.use_only_tar_bz2 else '') + '.q'

    def load(self):
        _internal_state = self._load()
        if _internal_state.get("repodata_version", 0) > MAX_REPODATA_VERSION:
            raise CondaUpgradeError(dals("""
                The current version of conda is too old to read repodata from

                    %s

                (This version only supports repodata_version 1.)
                Please update conda to use this channel.
                """) % self.url_w_repodata_fn)

        self._internal_state = _internal_state
        self._package_records = _internal_state['_package_records']
        self._names_index = _internal_state['_names_index']
        self._track_features_index = _internal_state['_track_features_index']
        self._loaded = True
        return self

    def iter_records(self):
        if not self._loaded:
            self.load()
        return iter(self._package_records)

    # replaces old in-band mod_and_etag headers
    def _load_state(self):
        try:
            state_path = pathlib.Path(self.cache_path_state)
            state = json.loads(state_path.read_text())
            log.debug("Load state from %s", state_path)
            return state
        except:
            log.debug("Could not load state", exc_info=True)
            return {}

    def _save_state(self, state):
        return pathlib.Path(self.cache_path_state).write_text(json.dumps(state))

    def _load(self):
        try:
            mtime = getmtime(self.cache_path_json)
        except (IOError, OSError):
            log.debug("No local cache found for %s at %s", self.url_w_repodata_fn,
                      self.cache_path_json)
            if context.use_index_cache or (context.offline
                                           and not self.url_w_subdir.startswith('file://')):
                log.debug("Using cached data for %s at %s forced. Returning empty repodata.",
                          self.url_w_repodata_fn, self.cache_path_json)
                return {
                    '_package_records': (),
                    '_names_index': defaultdict(list),
                    '_track_features_index': defaultdict(list),
                }
            else:
                mod_etag_headers = {}
        else:
            mod_etag_headers = self._load_state()

            if context.use_index_cache:
                log.debug("Using cached repodata for %s at %s because use_cache=True",
                          self.url_w_repodata_fn, self.cache_path_json)

                _internal_state = self._read_local_repodata(mod_etag_headers)
                return _internal_state

            if context.local_repodata_ttl > 1:
                max_age = context.local_repodata_ttl
            elif context.local_repodata_ttl == 1:
                max_age = get_cache_control_max_age(mod_etag_headers.get('_cache_control', ''))
            else:
                max_age = 0

            timeout = mtime + max_age - time()
            if (timeout > 0 or context.offline) and not self.url_w_subdir.startswith('file://'):
                log.debug("Using cached repodata for %s at %s. Timeout in %d sec",
                          self.url_w_repodata_fn, self.cache_path_json, timeout)
                _internal_state = self._read_local_repodata(mod_etag_headers)
                return _internal_state

            log.debug("Local cache timed out for %s at %s",
                      self.url_w_repodata_fn, self.cache_path_json)

        try:
            raw_repodata_str = self._repo.repodata(mod_etag_headers)
        except Response304ContentUnchanged:
            log.debug("304 NOT MODIFIED for '%s'. Updating mtime and loading from disk",
                      self.url_w_repodata_fn)
            touch(self.cache_path_json)
            _internal_state = self._read_local_repodata(mod_etag_headers)
            return _internal_state
        else:
            if not isdir(dirname(self.cache_path_json)):
                mkdir_p(dirname(self.cache_path_json))
            try:
                cache_path_json = self.cache_path_json
                with io_open(cache_path_json, 'w') as fh:
                    fh.write(raw_repodata_str or "{}")
                # quick thing to check for 'json matches stat', or store, check a message digest:
                mod_etag_headers["mtime"] = pathlib.Path(cache_path_json).stat().st_mtime
                self._save_state(mod_etag_headers)
            except (IOError, OSError) as e:
                if e.errno in (EACCES, EPERM, EROFS):
                    raise NotWritableError(self.cache_path_json, e.errno, caused_by=e)
                else:
                    raise
            _internal_state = self._process_raw_repodata_str(raw_repodata_str, mod_etag_headers)
            self._internal_state = _internal_state
            self._pickle_me()
            return _internal_state

    def _pickle_me(self):
        try:
            log.debug("Saving pickled state for %s at %s", self.url_w_repodata_fn,
                      self.cache_path_pickle)
            with open(self.cache_path_pickle, 'wb') as fh:
                pickle.dump(self._internal_state, fh, -1)  # -1 means HIGHEST_PROTOCOL
        except Exception:
            log.debug("Failed to dump pickled repodata.", exc_info=True)

    def _read_local_repodata(self, state):
        # first try reading pickled data
        _pickled_state = self._read_pickled(state)
        if _pickled_state:
            return _pickled_state

        # pickled data is bad or doesn't exist; load cached json
        log.debug("Loading raw json for %s at %s", self.url_w_repodata_fn, self.cache_path_json)

        # TODO allow repo plugin to load this data; don't require verbatim JSON on disk?
        with open(self.cache_path_json) as fh:
            try:
                raw_repodata_str = fh.read()
            except ValueError as e:
                # ValueError: Expecting object: line 11750 column 6 (char 303397)
                log.debug("Error for cache path: '%s'\n%r", self.cache_path_json, e)
                message = dals("""
                An error occurred when loading cached repodata.  Executing
                `conda clean --index-cache` will remove cached repodata files
                so they can be downloaded again.
                """)
                raise CondaError(message)
            else:
                _internal_state = self._process_raw_repodata_str(raw_repodata_str, self._load_state())
                self._internal_state = _internal_state
                self._pickle_me()
                return _internal_state

    def _read_pickled(self, state):

        if not isfile(self.cache_path_pickle) or not isfile(self.cache_path_json):
            # Don't trust pickled data if there is no accompanying json data
            return None

        try:
            if isfile(self.cache_path_pickle):
                log.debug("found pickle file %s", self.cache_path_pickle)
            with open(self.cache_path_pickle, 'rb') as fh:
                _pickled_state = pickle.load(fh)
        except Exception:
            log.debug("Failed to load pickled repodata.", exc_info=True)
            rm_rf(self.cache_path_pickle)
            return None

        def _pickle_valid_checks():
            yield '_url', _pickled_state.get('_url'), self.url_w_credentials
            yield '_schannel', _pickled_state.get('_schannel'), self.channel.canonical_name
            yield '_add_pip', _pickled_state.get('_add_pip'), context.add_pip_as_python_dependency
            yield '_mod', _pickled_state.get('_mod'), state.get('_mod')
            yield '_etag', _pickled_state.get('_etag'), state.get('_etag')
            yield '_pickle_version', _pickled_state.get('_pickle_version'), REPODATA_PICKLE_VERSION
            yield 'fn', _pickled_state.get('fn'), self.repodata_fn

        def _check_pickled_valid():
            for _, left, right in _pickle_valid_checks():
                yield left == right

        if not all(_check_pickled_valid()):
            log.debug("Pickle load validation failed for %s at %s. %r",
                      self.url_w_repodata_fn, self.cache_path_json, tuple(_pickle_valid_checks()))
            return None

        return _pickled_state

    def _process_raw_repodata_str(self, raw_repodata_str, state={}):
        """
        state contains information that was previously in-band in raw_repodata_str.
        """
        json_obj = json.loads(raw_repodata_str or '{}')
        return self._process_raw_repodata(json_obj, state=state)

    def _process_raw_repodata(self, repodata, state={}):
        subdir = repodata.get('info', {}).get('subdir') or self.channel.subdir
        assert subdir == self.channel.subdir
        add_pip = context.add_pip_as_python_dependency
        schannel = self.channel.canonical_name

        self._package_records = _package_records = []
        self._names_index = _names_index = defaultdict(list)
        self._track_features_index = _track_features_index = defaultdict(list)

        signatures = repodata.get("signatures", {})

        _internal_state = {
            'channel': self.channel,
            'url_w_subdir': self.url_w_subdir,
            'url_w_credentials': self.url_w_credentials,
            'cache_path_base': self.cache_path_base,
            'fn': self.repodata_fn,

            '_package_records': _package_records,
            '_names_index': _names_index,
            '_track_features_index': _track_features_index,

            '_etag': state.get('_etag'),
            '_mod': state.get('_mod'),
            '_cache_control': state.get('_cache_control'),
            '_url': state.get('_url'),
            '_add_pip': add_pip,
            '_pickle_version': REPODATA_PICKLE_VERSION,
            '_schannel': schannel,
            'repodata_version': state.get('repodata_version', 0),
        }
        if _internal_state["repodata_version"] > MAX_REPODATA_VERSION:
            raise CondaUpgradeError(dals("""
                The current version of conda is too old to read repodata from

                    %s

                (This version only supports repodata_version 1.)
                Please update conda to use this channel.
                """) % self.url_w_subdir)

        meta_in_common = {  # just need to make this once, then apply with .update()
            'arch': repodata.get('info', {}).get('arch'),
            'channel': self.channel,
            'platform': repodata.get('info', {}).get('platform'),
            'schannel': schannel,
            'subdir': subdir,
        }

        channel_url = self.url_w_credentials
        legacy_packages = repodata.get("packages", {})
        conda_packages = {} if context.use_only_tar_bz2 else repodata.get("packages.conda", {})

        _tar_bz2 = CONDA_PACKAGE_EXTENSION_V1
        use_these_legacy_keys = set(legacy_packages.keys()) - set(
            k[:-6] + _tar_bz2 for k in conda_packages.keys()
        )

        for group, copy_legacy_md5 in (
                (conda_packages.items(), True),
                (((k, legacy_packages[k]) for k in use_these_legacy_keys), False)):
            for fn, info in group:

                # Verify metadata signature before anything else so run-time
                # updates to the info dictionary performed below do not
                # invalidate the signatures provided in metadata.json.
                signature_verification(info, fn, signatures)

                info['fn'] = fn
                info['url'] = join_url(channel_url, fn)
                if copy_legacy_md5:
                    counterpart = fn.replace('.conda', '.tar.bz2')
                    if counterpart in legacy_packages:
                        info['legacy_bz2_md5'] = legacy_packages[counterpart].get('md5')
                        info['legacy_bz2_size'] = legacy_packages[counterpart].get('size')
                if (add_pip and info['name'] == 'python' and
                        info['version'].startswith(('2.', '3.'))):
                    info['depends'].append('pip')
                info.update(meta_in_common)
                if info.get('record_version', 0) > 1:
                    log.debug("Ignoring record_version %d from %s",
                              info["record_version"], info['url'])
                    continue

                package_record = PackageRecord(**info)

                _package_records.append(package_record)
                _names_index[package_record.name].append(package_record)
                for ftr_name in package_record.track_features:
                    _track_features_index[ftr_name].append(package_record)

        self._internal_state = _internal_state
        return _internal_state


def get_cache_control_max_age(cache_control_value):
    max_age = re.search(r"max-age=(\d+)", cache_control_value)
    return int(max_age.groups()[0]) if max_age else 0


def make_feature_record(feature_name):
    # necessary for the SAT solver to do the right thing with features
    pkg_name = "%s@" % feature_name
    return PackageRecord(
        name=pkg_name,
        version='0',
        build='0',
        channel='@',
        subdir=context.subdir,
        md5="12345678901234567890123456789012",
        track_features=(feature_name,),
        build_number=0,
        fn=pkg_name,
    )


def cache_fn_url(url, repodata_fn=REPODATA_FN):
    # url must be right-padded with '/' to not invalidate any existing caches
    if not url.endswith('/'):
        url += '/'
    # add the repodata_fn in for uniqueness, but keep it off for standard stuff.
    #    It would be more sane to add it for everything, but old programs (Navigator)
    #    are looking for the cache under keys without this.
    if repodata_fn != REPODATA_FN:
        url += repodata_fn

    hash = hashlib.sha256(ensure_binary(url))

    # multiples of 5 will produce unpadded base32 strings
    return '%s.json' % base64.b32hexencode(hash.digest()[:5]).decode('utf-8')


def create_cache_dir():
    cache_dir = join(PackageCacheData.first_writable().pkgs_dir, 'cache')
    mkdir_p_sudo_safe(cache_dir)
    return cache_dir
