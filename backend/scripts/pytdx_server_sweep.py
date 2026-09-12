"""pytdx server pool 逐台 capability 体检（纯网络只读诊断）。

目的：判断当前 pytdx historical bars 失败究竟是
  (a) server pool stale（老 IP 大面积失效），还是
  (b) 公网 TDX `get_security_bars` capability 整体不可用（而 xdxr 仍可用）。

关键前提：``PytdxAdapter.max_retries=3`` 意味着一次业务调用最多只实际验证 3 台
server，所以「30/30 PytdxSourceError」**不能**推断「全池 bars fail」。本脚本对
union 池逐台独立探测。

设计：
- 每台 server 一个独立 subprocess，硬超时 8s（连接超时 1.5s 不足以约束函数调用）。
- 每台连续探测 2 轮；只有两轮 SH/SZ bars 均 >= 10 根才记 STABLE_BARS_OK。
- capability 分开记录：bars 与 xdxr 在**同一连接内分别独立 try/except**，
  以便识别「bars 不可用但 xdxr 可用」这类分化。

本脚本只读网络，不连 DB / Redis，不改生产代码。

用法：
    python scripts/pytdx_server_sweep.py                # 全池 sweep
    python scripts/pytdx_server_sweep.py --output out.json
    python scripts/pytdx_server_sweep.py --probe-one HOST PORT   # 内部 subprocess 入口
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONNECT_TIMEOUT = 1.5
HARD_SERVER_TIMEOUT = 8.0
BARS_MIN_COUNT = 10
PASSES = 2

# SH 600519 / SZ 000001
PROBES = {
    "sh_bars": (9, 1, "600519"),
    "sz_bars": (9, 0, "000001"),
    "sh_xdxr": (None, 1, "600519"),
    "sz_xdxr": (None, 0, "000001"),
}

# ── server union ────────────────────────────────────────────────────
OUR_POOL: list[str] = [
    "119.147.212.81",
    "119.147.164.60",
    "14.215.128.18",
    "14.215.128.116",
    "101.133.156.38",
    "114.80.149.19",
    "115.238.90.165",
    "123.125.108.23",
    "180.153.18.170",
    "202.108.253.131",
]
CHANLUN_POOL: list[str] = [
    "115.238.56.198",
    "115.238.90.165",
    "180.153.18.170",
    "218.75.126.9",
    "60.12.136.250",
    "60.191.117.167",
    "jstdx.gtjas.com",
    "shtdx.gtjas.com",
    "sztdx.gtjas.com",
    "159.75.55.232",
]

PORT = 7709

CLASS_BOTH_OK = "BOTH_OK"
CLASS_BARS_ONLY = "BARS_OK"
CLASS_XDXR_ONLY = "XDXR_ONLY"
CLASS_UNSTABLE = "UNSTABLE"
CLASS_CONNECT_FAIL = "CONNECT_FAIL"
CLASS_FUNCTION_FAIL = "FUNCTION_FAIL"


@dataclass
class ProbeCall:
    count: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ServerProbeResult:
    host: str
    port: int
    sources: list[str] = field(default_factory=list)
    resolved_ip: str | None = None

    # 每轮 pass -> {sh_bars, sz_bars, sh_xdxr, sz_xdxr}
    passes: list[dict] = field(default_factory=list)
    connect_ms: list[float | None] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    classification: str = ""
    fastest_connect_ms: float | None = None


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _call(fn) -> ProbeCall:  # noqa: ANN001
    try:
        data = fn()
        return ProbeCall(count=len(data or []), error=None)
    except Exception as exc:  # noqa: BLE001
        return ProbeCall(count=None, error=type(exc).__name__)


def probe_once(host: str, port: int) -> dict:
    """单轮探测：connect + 4 个 capability（各自独立 try/except）。"""
    from pytdx.hq import TdxHq_API

    api = TdxHq_API(raise_exception=True, auto_retry=False)
    started = time.perf_counter()
    if not api.connect(host, port, time_out=CONNECT_TIMEOUT):
        raise RuntimeError("connect returned false")
    connect_ms = (time.perf_counter() - started) * 1000.0

    out: dict = {"connect_ms": round(connect_ms, 1)}
    try:
        for name, (cat, market, code) in PROBES.items():
            if cat is None:
                res = _call(lambda m=market, c=code: api.get_xdxr_info(m, c))
            else:
                res = _call(
                    lambda k=cat, m=market, c=code: api.get_security_bars(k, m, c, 0, 100)
                )
            out[name] = asdict(res)
    finally:
        try:
            api.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return out


def _pass_ran(pass_result: dict) -> bool:
    """该轮是否真的跑到了 4 个 capability（connect 成功）。"""
    return bool(pass_result) and "sh_bars" in pass_result and "sh_xdxr" in pass_result


def _bars_ok(pass_result: dict) -> bool:
    if not _pass_ran(pass_result):
        return False
    sh = (pass_result.get("sh_bars") or {}).get("count") or 0
    sz = (pass_result.get("sz_bars") or {}).get("count") or 0
    return sh >= BARS_MIN_COUNT and sz >= BARS_MIN_COUNT


def _xdxr_ok(pass_result: dict) -> bool:
    if not _pass_ran(pass_result):
        return False
    sh = pass_result.get("sh_xdxr") or {}
    sz = pass_result.get("sz_xdxr") or {}
    return sh.get("error") is None and sz.get("error") is None


def _classify(result: ServerProbeResult) -> str:
    if not any(t is not None for t in result.connect_ms):
        return CLASS_CONNECT_FAIL
    bars = [_bars_ok(p) for p in result.passes if _pass_ran(p)]
    xdxr = [_xdxr_ok(p) for p in result.passes if _pass_ran(p)]
    if len(bars) < PASSES:
        return CLASS_UNSTABLE  # 有一轮没连上 → 不稳定
    if all(bars) and all(xdxr):
        return CLASS_BOTH_OK
    if all(bars):
        return CLASS_BARS_ONLY
    if all(xdxr):
        return CLASS_XDXR_ONLY
    if any(bars) or any(xdxr):
        return CLASS_UNSTABLE
    return CLASS_FUNCTION_FAIL


def _run_child(host: str, port: int) -> dict:
    """在 subprocess 内探测单台（硬超时保护由父进程施加）。"""
    return probe_once(host, port)


def probe_server(host: str, port: int = PORT, passes: int = PASSES) -> ServerProbeResult:
    result = ServerProbeResult(host=host, port=port)
    result.resolved_ip = _resolve(host)
    script = str(Path(__file__).resolve())

    for _ in range(passes):
        try:
            proc = subprocess.run(
                [sys.executable, script, "--probe-one", host, str(port)],
                capture_output=True,
                text=True,
                timeout=HARD_SERVER_TIMEOUT,
            )
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
            if proc.returncode != 0 or not lines:
                result.passes.append({})
                result.connect_ms.append(None)
                result.errors.append(
                    f"child_rc={proc.returncode} stderr={proc.stderr.strip()[-300:]}"
                )
                continue
            payload = json.loads(lines[-1])
            result.passes.append(payload)
            result.connect_ms.append(payload.get("connect_ms"))
            if payload.get("connect_error"):
                result.errors.append(payload["connect_error"])
        except subprocess.TimeoutExpired:
            result.passes.append({})
            result.connect_ms.append(None)
            result.errors.append(f"hard_timeout>{HARD_SERVER_TIMEOUT}s")
        except Exception as exc:  # noqa: BLE001
            result.passes.append({})
            result.connect_ms.append(None)
            result.errors.append(f"{type(exc).__name__}: {exc}")

    ok_times = [t for t in result.connect_ms if t is not None]
    result.fastest_connect_ms = min(ok_times) if ok_times else None
    result.classification = _classify(result)
    return result


def _union() -> list[tuple[str, list[str]]]:
    order: list[str] = []
    sources: dict[str, list[str]] = {}
    for name, pool in (("our_pool", OUR_POOL), ("chanlun_pool", CHANLUN_POOL)):
        for host in pool:
            if host not in sources:
                sources[host] = []
                order.append(host)
            sources[host].append(name)
    return [(h, sources[h]) for h in order]


def _cell(result: ServerProbeResult, pass_idx: int, key: str) -> str:
    if pass_idx >= len(result.passes):
        return "-"
    call = result.passes[pass_idx].get(key)
    if not call:
        return "-"
    if call.get("error"):
        return f"E:{call['error']}"
    return str(call.get("count"))


def _pass_tag(pass_result: dict) -> str:
    """表内单轮标记：C=connect 失败，B=bars OK，X=xdxr OK。"""
    if not _pass_ran(pass_result):
        return "C"
    return ("B" if _bars_ok(pass_result) else "-") + (
        "X" if _xdxr_ok(pass_result) else "-"
    )


def _summary(results: list[ServerProbeResult], pool: str) -> dict:
    subset = [r for r in results if pool in r.sources]

    def _ran_all(r: ServerProbeResult) -> bool:
        return len([p for p in r.passes if _pass_ran(p)]) == PASSES

    connect_ok = [r for r in subset if any(_pass_ran(p) for p in r.passes)]
    stable_bars = [
        r for r in subset if _ran_all(r) and all(_bars_ok(p) for p in r.passes)
    ]
    xdxr_ok = [r for r in subset if _ran_all(r) and all(_xdxr_ok(p) for p in r.passes)]
    both_ok = [
        r
        for r in subset
        if _ran_all(r) and all(_bars_ok(p) and _xdxr_ok(p) for p in r.passes)
    ]
    return {
        "total": len(subset),
        "connect_ok": len(connect_ok),
        "stable_bars_ok": len(stable_bars),
        "xdxr_ok": len(xdxr_ok),
        "both_ok": len(both_ok),
        "stable_bars_servers": [r.host for r in stable_bars],
        "xdxr_servers": [r.host for r in xdxr_ok],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pytdx server pool capability sweep")
    parser.add_argument("--probe-one", nargs=2, metavar=("HOST", "PORT"))
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args(argv)

    if args.probe_one:
        host, port = args.probe_one[0], int(args.probe_one[1])
        try:
            print(json.dumps(probe_once(host, port)))
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "connect_ms": None,
                        "connect_error": f"{type(exc).__name__}: {exc}",
                    }
                )
            )
        return 0

    union = _union()
    print(f"union server count = {len(union)} (our_pool={len(OUR_POOL)}, chanlun_pool={len(CHANLUN_POOL)})")
    print(f"connect_timeout={CONNECT_TIMEOUT}s hard_server_timeout={HARD_SERVER_TIMEOUT}s passes={PASSES}")
    print()

    started = time.perf_counter()
    results: list[ServerProbeResult] = []
    for idx, (host, sources) in enumerate(union, 1):
        r = probe_server(host, PORT)
        r.sources = sources
        results.append(r)
        print(f"[{idx}/{len(union)}] {host} -> {r.classification} ({time.perf_counter() - started:.1f}s)", flush=True)
    elapsed = time.perf_counter() - started

    header = (
        f"{'source':14} {'server':20} {'resolved_ip':16} {'conn_ms':>8} "
        f"{'SHbars':>14} {'SZbars':>14} {'SHxdxr':>14} {'SZxdxr':>14} "
        f"{'p1':12} {'p2':12} {'classification':16}"
    )
    print()
    print(header)
    print("-" * len(header))
    for r in results:
        p1 = _pass_tag(r.passes[0]) if r.passes else "C"
        p2 = _pass_tag(r.passes[1]) if len(r.passes) > 1 else "C"
        print(
            f"{'/'.join(r.sources):14} {r.host:20} {str(r.resolved_ip):16} "
            f"{str(r.fastest_connect_ms):>8} "
            f"{_cell(r, 0, 'sh_bars'):>14} {_cell(r, 0, 'sz_bars'):>14} "
            f"{_cell(r, 0, 'sh_xdxr'):>14} {_cell(r, 0, 'sz_xdxr'):>14} "
            f"{p1:12} {p2:12} {r.classification:16}"
        )

    ours = _summary(results, "our_pool")
    theirs = _summary(results, "chanlun_pool")
    print()
    print("=== summary ===")
    print(json.dumps({"our_pool": ours, "chanlun_pool": theirs}, ensure_ascii=False, indent=2))
    print(f"\nsweep elapsed = {elapsed:.1f}s")

    payload = {
        "union_count": len(union),
        "connect_timeout": CONNECT_TIMEOUT,
        "hard_server_timeout": HARD_SERVER_TIMEOUT,
        "passes": PASSES,
        "elapsed_seconds": round(elapsed, 1),
        "servers": [
            {
                **asdict(r),
                "pass1_bars_ok": _bars_ok(r.passes[0]) if r.passes else None,
                "pass2_bars_ok": _bars_ok(r.passes[1]) if len(r.passes) > 1 else None,
            }
            for r in results
        ],
        "our_pool": ours,
        "chanlun_pool": theirs,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
