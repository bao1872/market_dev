"""Round 3 §12-13 — Specification Findings + ROUND3_SUMMARY + RECOMMENDED_PRD_CHANGES。

综合 §2-§11 的分析结果输出。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

FAMILY_CODES = ["P", "Q", "U", "C", "V"]

SPEC_CLASSES = [
    "SUPPORTED_CURRENT_DESIGN",
    "PARTIALLY_SUPPORTED",
    "SPECIFICATION_DEFECT_CANDIDATE",
    "MISSING_PRIMITIVE",
    "REDUNDANT_CANDIDATE",
    "INCONCLUSIVE",
]


def _classify_primitive_coverage(pc: dict[str, Any]) -> dict[str, str]:
    """把 primitive coverage verdict 映射到 spec finding 分类。"""
    out: dict[str, str] = {}
    for dim, info in pc.items():
        v = info.get("verdict")
        if v == "COVERED":
            out[dim] = "SUPPORTED_CURRENT_DESIGN"
        elif v == "PARTIAL":
            out[dim] = "PARTIALLY_SUPPORTED"
        elif v == "MISSING":
            out[dim] = "MISSING_PRIMITIVE"
        else:
            out[dim] = "INCONCLUSIVE"
    return out


def _classify_components(align_detail: dict[str, Any]) -> list[dict[str, Any]]:
    """从 alignment detail 提取 component-level findings。"""
    findings: list[dict[str, Any]] = []
    for cname, d in align_detail.items():
        f = d.get("finding", "")
        dim = d.get("semantic_dimension", "")
        entry: dict[str, Any] = {
            "component": cname,
            "family": d.get("family"),
            "semantic_dimension": dim,
        }
        if f == "COVERED":
            entry["class"] = "SUPPORTED_CURRENT_DESIGN"
        elif f == "PARTIAL":
            entry["class"] = "PARTIALLY_SUPPORTED"
        elif f == "MIXED":
            entry["class"] = "SPECIFICATION_DEFECT_CANDIDATE"
            entry["note"] = "component 混合多个独立 primitive 维度"
        elif f == "REDUNDANT_CANDIDATE":
            entry["class"] = "REDUNDANT_CANDIDATE"
            entry["redundant_with"] = d.get("same_family_redundant_pairs", [])
        elif f == "NO_DIRECT_COUNTERPART":
            # 如果 component 属于 STATE/TRANSITION 等高优先维度但无对应，标缺陷候选
            if dim in ("TRANSITION", "BREADTH"):
                entry["class"] = "MISSING_PRIMITIVE"
            else:
                entry["class"] = "INCONCLUSIVE"
        else:
            entry["class"] = "INCONCLUSIVE"
        findings.append(entry)
    return findings


def _classify_compression(comp_audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 compression audit 提取 family-level 分类。"""
    out: dict[str, dict[str, Any]] = {}
    for f, info in comp_audit.items():
        verdict = info.get("verdict", "INCONCLUSIVE")
        if verdict == "RETAIN":
            cls = "SUPPORTED_CURRENT_DESIGN"
        elif verdict == "RETAIN_AS_SUMMARY":
            cls = "PARTIALLY_SUPPORTED"
        elif verdict == "RESTRUCTURE_CANDIDATE":
            cls = "SPECIFICATION_DEFECT_CANDIDATE"
        else:
            cls = "INCONCLUSIVE"
        out[f] = {
            "class": cls,
            "verdict": verdict,
            "n_dimensions": info.get("n_dimensions"),
            "dimensions_spanned": info.get("dimensions_spanned"),
        }
    return out


def generate_specification_findings(
    adjacency: dict[str, Any] | None,
    primitive_cov: dict[str, Any],
    align_detail: dict[str, Any],
    comp_audit: dict[str, Any],
) -> dict[str, Any]:
    primitive_spec = _classify_primitive_coverage(primitive_cov)
    component_findings = _classify_components(align_detail)
    family_findings = _classify_compression(comp_audit)

    # 按 class 聚合统计
    counts: dict[str, int] = {c: 0 for c in SPEC_CLASSES}
    for entry in component_findings:
        counts[entry["class"]] = counts.get(entry["class"], 0) + 1

    # adjacency 结论
    adj_conclusion = (adjacency or {}).get("conclusion", "UNKNOWN")

    # 特别识别 Implementation Gap vs Specification Defect
    # - Implementation Gap: 有 PRD 要求但实现缺失（当前不涉及具体实现 gap）
    # - Specification Defect: PRD 设计层面问题（混合维度/压缩不合理）
    spec_defects = [e for e in component_findings
                    if e["class"] == "SPECIFICATION_DEFECT_CANDIDATE"]
    missing_prims = [dim for dim, cls in primitive_spec.items()
                     if cls == "MISSING_PRIMITIVE"]
    redundant_comps = [e["component"] for e in component_findings
                       if e["class"] == "REDUNDANT_CANDIDATE"]

    return {
        "adjacency_conclusion": adj_conclusion,
        "classification_counts": counts,
        "primitive_coverage_classification": primitive_spec,
        "family_level_classification": family_findings,
        "summary": {
            "specification_defect_candidates": spec_defects,
            "missing_primitives": missing_prims,
            "redundant_candidates": redundant_comps,
        },
        "component_findings": component_findings,
    }


def _render_recommended_changes(
    spec: dict[str, Any],
    comp_audit: dict[str, Any],
    primitive_cov: dict[str, Any],
) -> str:
    """生成 ROUND3_RECOMMENDED_PRD_CHANGES.md 内容。"""
    lines: list[str] = []
    lines.append("# ROUND 3 — RECOMMENDED PRD CHANGES (提案)")
    lines.append("")
    lines.append("> **仅为实验提案。不修改正式 PRD。**")
    lines.append("> 不写最终 frontend。不写正式实现方案。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. 保留的设计
    lines.append("## 1. 应保留的 Current Review 设计")
    lines.append("")
    kept_families = [f for f, info in spec["family_level_classification"].items()
                     if info["class"] in ("SUPPORTED_CURRENT_DESIGN", "PARTIALLY_SUPPORTED")]
    kept_components = [e["component"] for e in spec["component_findings"]
                       if e["class"] in ("SUPPORTED_CURRENT_DESIGN", "PARTIALLY_SUPPORTED")]
    lines.append(f"- 家族 Retain/RetainAsSummary: **{', '.join(kept_families)}**")
    lines.append(f"- 覆盖/部分覆盖的 components: **{len(kept_components)} / 27**")
    lines.append("  - 这些 component 与 Round 2 primitive 对齐良好或部分对齐。")
    lines.append("")

    # 2. Summary Layer
    lines.append("## 2. 应降为 Summary Layer 的设计")
    lines.append("")
    for f, info in comp_audit.items():
        if info["verdict"] == "RETAIN_AS_SUMMARY":
            lines.append(f"- **{f}**: verdict=RETAIN_AS_SUMMARY")
            lines.append(f"  - 跨维度: {info['dimensions_spanned']}")
            lines.append("  - 保留作为一眼快速参考，不再作为细致观察层唯一入口")
            lines.append("")

    # 3. 可能需要拆分/重构的 family
    lines.append("## 3. 可能需要拆分/重构的 family")
    lines.append("")
    for f, info in comp_audit.items():
        if info["verdict"] == "RESTRUCTURE_CANDIDATE":
            lines.append(f"- **{f}**: verdict=RESTRUCTURE_CANDIDATE")
            lines.append(f"  - 当前跨 {info['n_dimensions']} 个 primitive 维度: "
                         f"{info['dimensions_spanned']}")
            lines.append("  - component 内相关性低，加权平均会混合独立信号")
            lines.append("  - 建议：按 primitive 维度拆分为多个子观察指标，"
                         "family 保留 summary 角色")
            lines.append("")

    # 4. 应补入正式 Observation 层的 primitive
    lines.append("## 4. 应补入正式 Observation 层的 primitive")
    lines.append("")
    for dim, cls in spec["primitive_coverage_classification"].items():
        if cls in ("MISSING_PRIMITIVE", "PARTIALLY_SUPPORTED"):
            lines.append(f"- **{dim}**: {cls}")
            pinfo = primitive_cov.get(dim, {})
            direct = pinfo.get("direct_components", [])
            mixed = pinfo.get("mixed_components", [])
            if direct:
                lines.append(f"  - 直接表达 components: {[c['name'] for c in direct]}")
            if mixed:
                lines.append(f"  - 仅在 MIXED component 中触及: {[c['name'] for c in mixed]}")
            lines.append("")

    # 5. 可能冗余的旧 component
    lines.append("## 5. 可能冗余的旧 component")
    lines.append("")
    reds = spec["summary"].get("redundant_candidates", [])
    if reds:
        for cname in reds:
            lines.append(f"- **{cname}**: 与同 family 其他 component spearman ≥ 0.7")
        lines.append("")
        lines.append("> 需人工确认：冗余候选是同维度重复表达（可去重）还是"
                     "虽相关但有独立语义（保留）。")
    else:
        lines.append("- 本轮未发现明显冗余（阈值: 同family spearman ≥ 0.7）")
    lines.append("")

    # 6. 证据不足
    lines.append("## 6. 结论仍证据不足")
    lines.append("")
    incon = [e for e in spec["component_findings"] if e["class"] == "INCONCLUSIVE"]
    incon_dims = [dim for dim, cls in spec["primitive_coverage_classification"].items()
                  if cls == "INCONCLUSIVE"]
    if incon:
        lines.append(f"- 无法判断 component: {[e['component'] for e in incon]}")
    if incon_dims:
        lines.append(f"- 无法判断 primitive dimension: {incon_dims}")
    lines.append("")
    lines.append("> 主要原因：Round 2 primitive 列与 Review component 的维度映射 "
                 "需要更大样本或更多 primitive 标签列来确认。")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Generated by Round 3 alignment analysis.*")
    return "\n".join(lines) + "\n"


def _render_summary(
    adjacency: dict[str, Any] | None,
    matrix_rows: list[dict[str, Any]] | None,
    primitive_cov: dict[str, Any],
    spec: dict[str, Any],
    comp_audit: dict[str, Any],
    arche: dict[str, Any] | None,
    cross_horizon: dict[str, Any] | None,
) -> str:
    """生成 ROUND3_SUMMARY.md。"""
    lines: list[str] = []
    lines.append("# ROUND 3 — Existing Review Alignment Audit — SUMMARY")
    lines.append("")
    lines.append("**窗口**: 2026-02-09 .. 2026-08-10 | 120 trading dates")
    lines.append("")
    adj_conclusion = (adjacency or {}).get("conclusion", "UNKNOWN")
    lines.append(f"**Adjacency Conclusion**: **{adj_conclusion}**")
    lines.append("")

    # Adjacency 数字
    if adjacency:
        ta = adjacency.get("transition_adjacency", {})
        ca = adjacency.get("concentration_adjacency", {})
        lines.append("## §2 Adjacency Micro-check 结果")
        lines.append("")
        lines.append(f"- Transition eligible pairs: {ta.get('eligible_common_universe_pairs')}")
        lines.append(f"  - exact T-1: {ta.get('exact_Tminus1_pairs')}")
        lines.append(f"  - skipped: {ta.get('skipped_date_pairs')}")
        lines.append(f"  - skip ratio: {ta.get('skipped_date_pair_ratio')}")
        lines.append(f"- Concentration close pairs: {ca.get('total_close_pairs')}")
        lines.append(f"  - exact T-1: {ca.get('exact_Tminus1_pairs')}")
        lines.append(f"  - skipped: {ca.get('skipped_date_pairs')}")
        lines.append(f"  - skip ratio: {ca.get('skipped_date_pair_ratio')}")
        if adjacency.get("recompute_required"):
            lines.append("")
            lines.append("修正前后 rho 对比:")
            for key, info in (adjacency.get("rho_comparison") or {}).items():
                lines.append(
                    f"  - {key}: old={info.get('old')} "
                    f"corrected={info.get('corrected')} delta={info.get('delta')} "
                    f"verdict_changed={info.get('verdict_changed')}"
                )
        lines.append("")

    # Coverage 分布
    lines.append("## §7 Coverage Matrix Findings 分布")
    lines.append("")
    if matrix_rows:
        dist: dict[str, int] = {}
        for r in matrix_rows:
            f = r.get("finding", "")
            dist[f] = dist.get(f, 0) + 1
        for cls, n in dist.items():
            lines.append(f"- **{cls}**: {n} components")
    lines.append("")

    # Primitive coverage verdicts
    lines.append("## §8 Primitive Coverage")
    lines.append("")
    for dim, cls in spec["primitive_coverage_classification"].items():
        lines.append(f"- **{dim}**: {cls}")
    lines.append("")

    # §9 Family compression verdicts
    lines.append("## §9 P/Q/U/C/V Compression Audit Verdict")
    lines.append("")
    for f, info in comp_audit.items():
        verdict = info.get("verdict", "?")
        n_dims = info.get("n_dimensions", "?")
        dims = info.get("dimensions_spanned", [])
        lines.append(f"- **{f}**: {verdict} (spans {n_dims} dims: {dims})")
    lines.append("")

    # §10 Archetype clarity
    lines.append("## §10 Archetype Day Clarity")
    lines.append("")
    if arche:
        clarity: dict[str, int] = {"CLEAR": 0, "PARTIAL": 0, "OBSCURED": 0,
                                    "DATE_NOT_FOUND": 0}
        for d, info in arche.items():
            v = info.get("clarity_verdict") or info.get("status") or "UNKNOWN"
            clarity[v] = clarity.get(v, 0) + 1
        for k, n in clarity.items():
            lines.append(f"- {k}: {n} days")
    lines.append("")

    # §12 Specification findings
    lines.append("## §12 Specification Findings 总览")
    lines.append("")
    for cls, n in (spec.get("classification_counts") or {}).items():
        lines.append(f"- {cls}: {n}")
    lines.append("")
    summary = spec.get("summary") or {}
    if summary.get("specification_defect_candidates"):
        lines.append("**Specification Defect candidates (混合多维度)**:")
        for e in summary["specification_defect_candidates"]:
            lines.append(f"  - {e['component']} ({e.get('family')}, {e.get('semantic_dimension')})")
    if summary.get("missing_primitives"):
        lines.append(f"**Missing primitives**: {summary['missing_primitives']}")
    if summary.get("redundant_candidates"):
        lines.append(f"**Redundant candidates**: {summary['redundant_candidates']}")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*详细数据文件见 round3_*.json/csv。*")
    return "\n".join(lines) + "\n"


def run_summary(out_root: Path) -> dict[str, Any]:
    out_r3 = out_root / "round3"

    # 读所有输入
    adj_path = out_r3 / "round2_adjacency_check.json"
    adjacency = None
    if adj_path.exists():
        adjacency = json.loads(adj_path.read_text())

    pc_path = out_r3 / "round3_primitive_coverage.json"
    primitive_cov = json.loads(pc_path.read_text()) if pc_path.exists() else {}

    detail_path = out_r3 / "round3_component_alignment_detail.json"
    align_detail = json.loads(detail_path.read_text()) if detail_path.exists() else {}

    matrix_rows: list[dict[str, Any]] | None = None
    matrix_csv = out_r3 / "round3_alignment_matrix.csv"
    if matrix_csv.exists():
        import csv as _csv
        with open(matrix_csv, "r", encoding="utf-8") as f:
            matrix_rows = list(_csv.DictReader(f))

    comp_path = out_r3 / "round3_family_compression_audit.json"
    comp_audit = json.loads(comp_path.read_text()) if comp_path.exists() else {}

    arche_path = out_r3 / "round3_archetype_replay.json"
    arche = json.loads(arche_path.read_text()) if arche_path.exists() else None

    ch_path = out_r3 / "round3_cross_horizon_replay.json"
    cross_horizon = json.loads(ch_path.read_text()) if ch_path.exists() else None

    spec = generate_specification_findings(
        adjacency, primitive_cov, align_detail, comp_audit,
    )
    (out_r3 / "round3_specification_findings.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False, default=str)
    )

    summary_md = _render_summary(
        adjacency, matrix_rows, primitive_cov, spec, comp_audit, arche, cross_horizon,
    )
    (out_root / "ROUND3_SUMMARY.md").write_text(summary_md, encoding="utf-8")

    prd_md = _render_recommended_changes(spec, comp_audit, primitive_cov)
    (out_root / "ROUND3_RECOMMENDED_PRD_CHANGES.md").write_text(prd_md, encoding="utf-8")

    return {
        "specification_counts": spec["classification_counts"],
        "adjacency_conclusion": spec["adjacency_conclusion"],
        "reports_written": [
            "round3_specification_findings.json",
            "ROUND3_SUMMARY.md",
            "ROUND3_RECOMMENDED_PRD_CHANGES.md",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "out")
    args = parser.parse_args()
    result = run_summary(args.out_root)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
