# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
# Lappin' up the jlap
from __future__ import annotations
import io

import json
import logging
import pathlib
import pprint
import re
import time
import zstandard
from contextlib import contextmanager
from hashlib import blake2b
from typing import Iterator

import jsonpatch
from requests import HTTPError

from conda.base.context import context
from conda.gateways.connection import Response, Session
from conda.gateways.repodata import RepodataState

from .jlapcore import jlap_buffer

log = logging.getLogger(__name__)


DIGEST_SIZE = 32  # 160 bits a minimum 'for security' length?

JLAP = "jlap"
HEADERS = "headers"
NOMINAL_HASH = "nominal_hash"
ON_DISK_HASH = "actual_hash"
LATEST = "latest"
JLAP_UNAVAILABLE = "jlap_unavailable"
ZSTD_UNAVAILABLE = "zstd_unavailable"


def hash():
    """
    Ordinary hash.
    """
    return blake2b(digest_size=DIGEST_SIZE)


def get_place(url, extra=""):
    if "current_repodata" in url:
        extra = f".c{extra}"
    return pathlib.Path("-".join(url.split("/")[-3:-1])).with_suffix(f"{extra}.json")


class Jlap304NotModified(Exception):
    pass


def process_jlap_response(response: Response, pos=0, iv=b""):
    # if response is 304 Not Modified, could return a buffer with only the
    # cached footer...
    if response.status_code == 304:
        raise Jlap304NotModified()

    def lines() -> Iterator[bytes]:
        yield from response.iter_lines(delimiter=b"\n")  # type: ignore

    buffer = jlap_buffer(lines(), iv, pos)

    # new iv == initial iv if nothing changed
    pos, footer, _ = buffer[-2]
    footer = json.loads(footer)

    new_state = {
        "headers": {k.lower(): v for k, v in response.headers.items()},
        "iv": buffer[-3][-1],
        "pos": pos,
        "footer": footer,
    }

    return buffer, new_state


def fetch_jlap(url, pos=0, etag=None, iv=b"", ignore_etag=True, session=None):
    response = request_jlap(url, pos=pos, etag=etag, ignore_etag=ignore_etag, session=session)
    return process_jlap_response(response, pos=pos, iv=iv)


def request_jlap(url, pos=0, etag=None, ignore_etag=True, session: Session | None = None):
    """
    Return the part of the remote .jlap file we are interested in.
    """
    # XXX max-age seconds; return dummy buffer if 304 not modified?
    headers = {}
    if pos:
        headers["range"] = f"bytes={pos}-"
    if etag and not ignore_etag:
        headers["if-none-match"] = etag

    log.debug("%s %s", url, headers)

    assert session is not None

    timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
    response = session.get(url, stream=True, headers=headers, timeout=timeout)
    response.raise_for_status()

    log.debug("request headers: %s", pprint.pformat(response.request.headers))
    log.debug(
        "response headers: %s",
        pprint.pformat(
            {
                k: v
                for k, v in response.headers.items()
                if any(map(k.lower().__contains__, ("content", "last", "range", "encoding")))
            }
        ),
    )
    pprint.pprint("status: %d" % response.status_code)
    if "range" in headers:
        # 200 is also a possibility that we'd rather not deal with; if the
        # server can't do range requests, also mark jlap as unavailable. Which
        # status codes mean 'try again' instead of 'it will never work'?
        if response.status_code not in (206, 304, 404, 416):
            raise HTTPError(
                f"Unexpected response code for range request {response.status_code}",
                response=response,
            )

    log.info("%s", response)

    return response


def hf(hash):
    """
    Abbreviate hash for formatting.
    """
    return hash[:16] + "\N{HORIZONTAL ELLIPSIS}"


def find_patches(patches, have, want):
    apply = []
    for patch in reversed(patches):
        if have == want:
            break
        if patch["to"] == want:
            log.info("Collect %s \N{LEFTWARDS ARROW} %s", hf(want), hf(patch["from"]))
            apply.append(patch)
            want = patch["from"]

    if have != want:
        print(f"No patch from local revision {hf(have)}")
        raise LookupError("patch not found")

    return apply


def apply_patches(data, apply):
    while apply:
        patch = apply.pop()
        print(
            f"{hf(patch['from'])} \N{RIGHTWARDS ARROW} {hf(patch['to'])}, "
            f"{len(patch['patch'])} steps"
        )
        data = jsonpatch.JsonPatch(patch["patch"]).apply(data, in_place=True)


def withext(url, ext):
    return re.sub(r"(\.\w+)$", ext, url)


@contextmanager
def timeme(message):
    begin = time.monotonic()
    yield
    end = time.monotonic()
    log.info("%sTook %0.02fs", message, end - begin)


def download_and_hash(hasher, url, json_path, session: Session, state: RepodataState | None):
    """
    Download url if it doesn't exist, passing bytes through hasher.update()
    """
    state = state or RepodataState()
    headers = {}

    # XXX check cache-control May be caller's job to compare with 'have_hash'
    # saved on previous download to detect cache tampering
    if json_path.exists():
        etag = state.get("_etag")
        if etag:
            headers["if-none-match"] = etag

    timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
    response = session.get(url, stream=True, timeout=timeout, headers=headers)
    log.debug("%s %s", url, response.headers)
    response.raise_for_status()
    length = 0
    # is there a status code for which we must clear the file?
    if response.status_code == 200:
        with json_path.open("wb") as repodata:
            for block in response.iter_content(chunk_size=1 << 14):
                hasher.update(block)
                repodata.write(block)
                length += len(block)
    if response.request:
        log.info("Download %d bytes %r", length, response.request.headers)
    return response  # can be 304 not modified


class HashWriter(io.RawIOBase):
    def __init__(self, backing, hasher):
        self.backing = backing
        self.hasher = hasher

    def write(self, b: bytes):
        self.hasher.update(b)
        return self.backing.write(b)

    def close(self):
        self.backing.close()


def download_and_hash_zst(hasher, url, json_path, session: Session, state: RepodataState | None):
    """
    Download url if it doesn't exist, passing bytes through zstandard
    decompression then hasher.update()
    """
    state = state or RepodataState()
    headers = {}

    # XXX check cache-control. May be caller's job to compare with 'have_hash'
    # saved on previous download to detect cache tampering
    if json_path.exists():
        etag = state.get("_etag")
        if etag:
            headers["if-none-match"] = etag

    timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
    response = session.get(url, stream=True, timeout=timeout, headers=headers)
    log.debug("%s %s", url, response.headers)
    response.raise_for_status()
    length = 0
    # is there a status code for which we must clear the file?
    if response.status_code == 200:
        decompressor = zstandard.ZstdDecompressor()
        with decompressor.stream_writer(
            HashWriter(json_path.open("wb"), hasher), closefd=True  # type: ignore
        ) as repodata:
            for block in response.iter_content(chunk_size=1 << 14):
                repodata.write(block)
                length += len(block)
    if response.request:
        log.info("Download %d bytes %r", length, response.request.headers)
    return response  # can be 304 not modified


def request_url_jlap_state(
    url, state: RepodataState, get_place=get_place, full_download=False, *, session: Session
):

    jlap_state = state.get(JLAP, {})
    headers = jlap_state.get(HEADERS, {})

    json_path = get_place(url)

    buffer = []  # type checks

    if (
        full_download
        or not (NOMINAL_HASH in state and json_path.exists())
        or state.get(JLAP_UNAVAILABLE)
    ):
        hasher = hash()
        with timeme(f"Download complete {url} "):

            # Don't deal with 304 Not Modified if hash unavailable e.g. if
            # cached without jlap
            if NOMINAL_HASH not in state:
                state.pop("etag", None)
                state.pop("mod", None)

            try:
                if state.should_check_format("zst"):
                    response = download_and_hash_zst(
                        hasher, withext(url, ".json.zst"), json_path, session=session, state=state
                    )
                else:
                    raise HTTPError("skip zst")
            except HTTPError as e:
                if e.response.status_code != 404:
                    raise
                state[ZSTD_UNAVAILABLE] = time.time_ns()
                response = download_and_hash(
                    hasher, withext(url, ".json"), json_path, session=session, state=state
                )

            # will we use state['headers'] for caching against
            state["_mod"] = response.headers.get("last-modified")
            state["_etag"] = response.headers.get("etag")
            state["_cache_control"] = response.headers.get("cache-control")

        # was not re-hashed if 304 not modified
        if response.status_code == 200:
            state[NOMINAL_HASH] = state[ON_DISK_HASH] = hasher.hexdigest()

        have = state[NOMINAL_HASH]

        # a jlap buffer with zero patches.
        buffer = [[-1, b"", ""], [0, json.dumps({LATEST: have}), ""], [1, b"", ""]]

    else:
        have = state[NOMINAL_HASH]
        # have_hash = state.get(ON_DISK_HASH)

        need_jlap = True
        try:
            # wrong to read state outside of function, and totally rebuild inside
            buffer, jlap_state = fetch_jlap(
                withext(url, ".jlap"),
                pos=jlap_state.get("pos", 0),
                etag=headers.get("etag", None),
                iv=bytes.fromhex(jlap_state.get("iv", "")),
                session=session,
                ignore_etag=False,
            )
            state.set_has_format("jlap", True)
            need_jlap = False
        except ValueError:
            log.info("Checksum not OK")
        except IndexError as e:
            log.info("Incomplete file?", exc_info=e)
        except HTTPError as e:
            # If we get a 416 Requested range not satisfiable, the server-side
            # file may have been truncated and we need to fetch from 0
            if e.response.status_code == 404:
                state[JLAP_UNAVAILABLE] = time.time_ns()  # XXX alternative method
                state.set_has_format("jlap", False)
                return request_url_jlap_state(
                    url, state, get_place=get_place, full_download=True, session=session
                )
            log.exception("Requests error")

        if need_jlap:  # retry whole file, if range failed
            try:
                buffer, jlap_state = fetch_jlap(withext(url, ".jlap"), session=session)
            except (ValueError, IndexError) as e:
                log.exception("Error parsing jlap", exc_info=e)
                # a 'latest' hash that we can't achieve, triggering later error handling
                buffer = [[-1, "", ""], [0, json.dumps({LATEST: "0" * 32}), ""], [1, "", ""]]
                state[JLAP_UNAVAILABLE] = time.time_ns()  # alternative method
                state.set_has_format("jlap", False)

        state[JLAP] = jlap_state

    with timeme("Apply Patches "):
        # buffer[0] == previous iv
        # buffer[1:-2] == patches
        # buffer[-2] == footer = new_state["footer"]
        # buffer[-1] == trailing checksum

        patches = list(json.loads(patch) for _, patch, _ in buffer[1:-2])
        want = json.loads(buffer[-2][1])["latest"]

        try:
            apply = find_patches(patches, have, want)
            log.info(f"Apply {len(apply)} patches {hf(have)} \N{RIGHTWARDS ARROW} {hf(want)}")

            if apply:
                with timeme("Load "), json_path.open() as repodata:
                    # we haven't loaded repodata yet; it could fail to parse, or
                    # have the wrong hash.
                    repodata_json = json.load(repodata)  # check have_hash here
                    # if this fails, then we also need to fetch again from 0

                apply_patches(repodata_json, apply)

                with timeme("Write changed "), json_path.open("wb") as repodata:

                    hasher = hash()

                    class hashwriter:
                        def write(self, s):
                            b = s.encode("utf-8")
                            hasher.update(b)
                            return repodata.write(b)

                    # faster than dump(obj, fp)
                    hashwriter().write(json.dumps(repodata_json))

                    # actual hash of serialized json
                    state[ON_DISK_HASH] = hasher.hexdigest()

                    # hash of equivalent upstream json
                    state[NOMINAL_HASH] = want

            else:
                assert state[NOMINAL_HASH] == want

        except (LookupError, json.JSONDecodeError) as e:
            if isinstance(e, LookupError):
                if e.args[0] != "patch not found":
                    raise
                # 'have' hash not mentioned in patchset
                #
                # XXX or skip jlap at top of fn; make sure it is not
                # possible to download the complete json twice
                log.info(
                    "Current repodata.json %s not found in patchset. Re-download repodata.json"
                )

            assert not full_download, "Recursion error"  # pragma: no cover

            # XXX debugging
            json_new_path = json_path.with_suffix(".json.old")
            log.warning("Rename to %s for debugging", json_new_path)
            json_path.rename(json_new_path)

            return request_url_jlap_state(
                url, state, get_place=get_place, full_download=True, session=session
            )
