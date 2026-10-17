"""Reuse the chart's exact TS ZigZag owner for snapshot direction and geometry."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import selectors
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).parents[1]/'strategy_integration'
_process = None
_pid = None
_lock = threading.Lock()


@lru_cache(maxsize=1)
def identity():
    manifest = json.loads((_ROOT/'tv_zigzag_core.json').read_text())
    if hashlib.sha256((_ROOT/'tv_zigzag_core.cjs').read_bytes()).hexdigest() != manifest['bundle_sha256']:
        raise ValueError('TV_ZIGZAG_CORE_INTEGRITY_FAILED')
    return manifest['source_sha256']


def _close():
    global _process
    if _process is not None:
        if _process.poll() is None:
            _process.terminate()
            try:
                _process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _process.kill()
                _process.wait()
        _process = None


atexit.register(_close)


def _request(raw, mode):
    global _process, _pid
    with _lock:
        if _process is None or _pid != os.getpid() or _process.poll() is not None:
            identity()
            _process = subprocess.Popen(['node', str(_ROOT/'tv_zigzag_bridge.cjs')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            os.set_blocking(_process.stdin.fileno(), False)
            os.set_blocking(_process.stdout.fileno(), False)
            _pid = os.getpid()
        payload = {'mode': mode, 'bars': raw[['high','low','volume']].to_numpy(float).tolist()}
        pending = (json.dumps(payload, allow_nan=False, separators=(',', ':'))+'\n').encode()
        sent, response, deadline = 0, bytearray(), time.monotonic()+15
        with selectors.DefaultSelector() as selector:
            selector.register(_process.stdin, selectors.EVENT_WRITE)
            selector.register(_process.stdout, selectors.EVENT_READ)
            while not response.endswith(b'\n'):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    _close()
                    raise ValueError('TV_ZIGZAG_RUNTIME_TIMEOUT')
                for key, _ in selector.select(timeout=remaining):
                    if key.fileobj is _process.stdin:
                        sent += os.write(_process.stdin.fileno(), pending[sent:sent+65536])
                        if sent == len(pending):
                            selector.unregister(_process.stdin)
                    else:
                        chunk = os.read(_process.stdout.fileno(), 65536)
                        if not chunk or len(response)+len(chunk) > 4*1024*1024:
                            _close()
                            raise ValueError('TV_ZIGZAG_RESPONSE_INCOMPLETE')
                        response.extend(chunk)
        value = json.loads(response)
        if 'error' in value:
            raise ValueError(value['error'])
        return value


def current_segment(raw):
    """Projection wins when present; otherwise the final solid segment wins."""
    value = _request(raw, 'segment')
    segment = value['segment']
    if segment is None:
        return None
    start, end = segment['from'], segment['to']
    sessions = end['index']-start['index']
    if sessions < 0 or end['price'] == start['price']:
        return None
    gain = (end['price']/start['price']-1)*100
    return {'direction': '向上' if end['price'] > start['price'] else '向下',
        'anchorDate': str(raw.datetime.iloc[start['index']].date()),
        'lastDate': str(raw.datetime.iloc[end['index']].date()),
        'asOf': str(raw.datetime.iloc[-1].date()), 'sessions': sessions,
        'totalReturnPct': gain, 'slopePctPerSession': gain/sessions if sessions else None,
        'fromPrice': start['price'], 'toPrice': end['price'],
        'toKind': 'H' if end['kind'] == 'high' else 'L',
        'fromIndex': start['index'], 'toIndex': end['index'],
        'lineStyle': 'dashed' if value['projected'] else 'solid',
        'endpointStatus': 'PROJECTED' if value['projected'] else 'RIGHT_LEG_VISIBLE_REPAINTABLE',
        'directionBasis': 'TV_LAST_SEGMENT', 'algorithmSha': identity()}


def overlay(raw):
    value = _request(raw, 'overlay')
    def point(p):
        return {**p, 'date': str(raw.datetime.iloc[p['index']].date())}
    def leg(s):
        return {**s, 'from': point(s['from']), 'to': point(s['to'])}
    return {'owner': 'frontend/src/components/zigzag.ts', 'algorithmSha': identity(),
        'asOf': str(raw.datetime.iloc[-1].date()), 'barCount': len(raw),
        'pivots': [point(p) for p in value['pivots']],
        'segments': [leg(s) for s in value['segments']],
        'projected': leg(value['projected']) if value['projected'] else None}
