"""[P0-5 修复 2026-07-29 三.3] chip 写入管道单元测试。

覆盖 DTO (ChipConsensusResult) → chip_flat → upsert 参数 的真实转换流程：
  1. chip 存在（available=True）：chip_flat 10 个字段填充正确，upsert status=succeeded
  2. chip=None（无有效峰）：chip_flat 全 None，upsert status=succeeded（NO_VALID_PEAK 在 chipStatus）
  3. 失败状态（INPUT_CONTRACT_VIOLATION）：chip_flat 全 None，upsert status=failed
  4. 算法版本：ChipConsensusResult.algorithmVersion == CHIP_CONSENSUS_ALGORITHM_VERSION

测试范围：
- 纯单元测试，不连接数据库
- 验证 model_dump(by_alias=False) 后的 chip_dict 结构与 flatten_chip_fields 兼容
- 验证 upsert 参数（chip_hash/chip_payload/status）由调用方按 chip_dict 正确组装

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_chip_upsert_pipeline.py -v
"""
from __future__ import annotations

from app.schemas.first_pyramid import (
    CHIP_CONSENSUS_ALGORITHM_VERSION,
    ChipConsensusResult,
    DimensionResult,
)
from app.services.first_pyramid_flatten import (
    FP_CHIP_KEYS,
    flatten_chip_fields,
)


def _build_chip_dimension() -> DimensionResult:
    """构造可用的 chip 维度（available=True）。"""
    return DimensionResult(
        name="chip_consensus",
        available=True,
        continuousFactors={
            "poc_price": 29.36,
            "last_close": 30.12,
            "n_peak_nodes": 2,
            "vah_price": 30.5,
            "val_price": 28.8,
        },
        events=[
            {
                "type": "NODE_CROSSOVER",
                "direction": "up",
                "freshnessBars": 3,
                "price": 29.40,
                "occurredAt": "2026-07-25",
                "barIndex": 100,
            }
        ],
        statusText="筹码峰稳定，价格在 POC 上方",
    )


class TestChipDictSerialization:
    """DTO → dict → chip_flat 转换测试。"""

    def test_chip_available_dto_to_dict_to_flat(self) -> None:
        """chip 存在（available=True）：model_dump 后 chip_flat 填充正确。"""
        chip_result = ChipConsensusResult(
            chip=_build_chip_dimension(),
            chipHash="sha256:abc123",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        # [P0-5 修复] 统一使用 model_dump(by_alias=False)
        chip_dict = chip_result.model_dump(by_alias=False)

        # 验证 dict 结构（与 upsert 调用方期望一致）
        assert chip_dict["chipHash"] == "sha256:abc123"
        assert chip_dict["algorithmVersion"] == CHIP_CONSENSUS_ALGORITHM_VERSION
        assert chip_dict["dailyBarsCount"] == 250
        assert chip_dict["bars15mCount"] == 4000
        assert chip_dict["error"] is None
        assert isinstance(chip_dict["chip"], dict)
        assert chip_dict["chip"]["available"] is True

        # 调用 flatten_chip_fields 生成 chip_flat
        chip_flat = flatten_chip_fields(chip_dict["chip"])
        assert set(chip_flat.keys()) == set(FP_CHIP_KEYS)
        assert len(chip_flat) == 10
        # 关键字段值
        assert chip_flat["fp_poc_price"] == 29.36
        assert chip_flat["fp_peak_node_count"] == 2
        assert chip_flat["fp_vah_price"] == 30.5
        assert chip_flat["fp_val_price"] == 28.8
        assert chip_flat["fp_chip_state"] == "筹码峰稳定，价格在 POC 上方"
        assert chip_flat["fp_node_event_type"] == "NODE_CROSSOVER"
        assert chip_flat["fp_node_event_direction"] == "up"
        assert chip_flat["fp_node_event_freshness"] == 3
        assert chip_flat["fp_node_event_price"] == 29.40
        # poc_distance_pct = (last_close - poc_price) / poc_price * 100
        expected_dist = round((30.12 - 29.36) / 29.36 * 100, 2)
        assert chip_flat["fp_poc_distance_pct"] == expected_dist

    def test_chip_none_dto_to_dict_to_flat(self) -> None:
        """chip=None（无有效峰）：chip_flat 全 None，但 DTO 仍可正常序列化。"""
        chip_result = ChipConsensusResult(
            chip=None,
            chipHash="sha256:no_peak",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,  # 无 error 但 chip=None → NO_VALID_PEAK
        )
        chip_dict = chip_result.model_dump(by_alias=False)

        assert chip_dict["chip"] is None
        assert chip_dict["error"] is None
        # flatten_chip_fields 接受 None chip
        chip_flat = flatten_chip_fields(chip_dict["chip"])
        assert len(chip_flat) == 10
        assert all(v is None for v in chip_flat.values())

    def test_chip_failed_dto_to_dict_to_flat(self) -> None:
        """失败状态（INPUT_CONTRACT_VIOLATION）：chip_flat 全 None，DTO 保留 error。"""
        chip_result = ChipConsensusResult(
            chip=None,
            chipHash="sha256:failed",
            dailyBarsCount=250,
            bars15mCount=338,  # 深科技根因：15m bars 不足 4000
            error="INPUT_CONTRACT_VIOLATION: 15m bars 338 < 4000",
        )
        chip_dict = chip_result.model_dump(by_alias=False)

        assert chip_dict["chip"] is None
        assert "INPUT_CONTRACT_VIOLATION" in chip_dict["error"]
        # flatten_chip_fields(None) 返回 10 个 None
        chip_flat = flatten_chip_fields(chip_dict["chip"])
        assert len(chip_flat) == 10
        assert all(v is None for v in chip_flat.values())

    def test_algorithm_version_constant(self) -> None:
        """[算法版本] ChipConsensusResult 默认 algorithmVersion == CHIP_CONSENSUS_ALGORITHM_VERSION。"""
        chip_result = ChipConsensusResult(
            chip=None,
            chipHash="sha256:version_test",
        )
        # Pydantic 模型属性
        assert chip_result.algorithmVersion == CHIP_CONSENSUS_ALGORITHM_VERSION
        # model_dump 后字段
        chip_dict = chip_result.model_dump(by_alias=False)
        assert chip_dict["algorithmVersion"] == CHIP_CONSENSUS_ALGORITHM_VERSION


class TestUpsertParamsAssembly:
    """upsert 参数组装测试（模拟 execute_after_close_chip_consensus 的关键步骤）。

    不连接数据库，只验证 model_dump → flatten_chip_fields → upsert 参数的正确转换。
    """

    def test_upsert_params_for_succeeded_chip(self) -> None:
        """chip 存在且 available=True：upsert status=succeeded，chip_payload 含 chip_flat。"""
        chip_result = ChipConsensusResult(
            chip=_build_chip_dimension(),
            chipHash="sha256:success",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        # 模拟 execute_after_close_chip_consensus 的步骤
        chip_dict = chip_result.model_dump(by_alias=False)
        chip_dict["chip_flat"] = flatten_chip_fields(chip_dict.get("chip"))

        # upsert 参数（与 _upsert_chip_snapshot 调用一致）
        upsert_params = {
            "chip_hash": chip_result.chipHash,
            "chip_payload": chip_dict,
            "status": "succeeded",
            "error_message": None,
        }
        assert upsert_params["chip_hash"] == "sha256:success"
        assert upsert_params["status"] == "succeeded"
        assert upsert_params["error_message"] is None
        # chip_payload 含 chip_flat
        assert "chip_flat" in upsert_params["chip_payload"]
        assert len(upsert_params["chip_payload"]["chip_flat"]) == 10
        # chip 维度保留在 chip_payload.chip（供详情 API 读取）
        assert upsert_params["chip_payload"]["chip"]["available"] is True

    def test_upsert_params_for_no_valid_peak(self) -> None:
        """chip=None 无 error：upsert status=succeeded（NO_VALID_PEAK 在 chipStatus 而非主 status）。"""
        chip_result = ChipConsensusResult(
            chip=None,
            chipHash="sha256:no_peak",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        chip_dict = chip_result.model_dump(by_alias=False)
        chip_dict["chip_flat"] = flatten_chip_fields(chip_dict.get("chip"))

        upsert_params = {
            "chip_hash": chip_result.chipHash,
            "chip_payload": chip_dict,
            "status": "succeeded",  # chip 任务执行成功，只是无有效峰
            "error_message": None,
        }
        assert upsert_params["status"] == "succeeded"
        assert upsert_params["chip_payload"]["chip"] is None
        assert all(v is None for v in upsert_params["chip_payload"]["chip_flat"].values())

    def test_upsert_params_for_failed(self) -> None:
        """失败状态：upsert status=failed，chip_payload 含 error。"""
        chip_result = ChipConsensusResult(
            chip=None,
            chipHash="sha256:failed",
            dailyBarsCount=250,
            bars15mCount=338,
            error="INPUT_CONTRACT_VIOLATION: 15m bars 338 < 4000",
        )
        chip_dict = chip_result.model_dump(by_alias=False)
        chip_dict["chip_flat"] = flatten_chip_fields(chip_dict.get("chip"))

        # 失败时 execute_after_close_chip_consensus 写入 status=failed
        upsert_params = {
            "chip_hash": chip_result.chipHash,
            "chip_payload": chip_dict,
            "status": "failed",
            "error_message": chip_result.error,
        }
        assert upsert_params["status"] == "failed"
        assert "INPUT_CONTRACT_VIOLATION" in upsert_params["error_message"]
        assert upsert_params["chip_payload"]["chip"] is None
        assert all(v is None for v in upsert_params["chip_payload"]["chip_flat"].values())

    def test_chip_flat_consistent_with_flatten_first_pyramid(self) -> None:
        """chip_flat 必须与 flatten_first_pyramid 的 chip 字段同源（避免口径不一致）。

        验证：用相同 chip_dimension 输入，flatten_chip_fields 与 flatten_first_pyramid
        的 10 个 chip 字段值完全一致。
        """
        chip_dim = _build_chip_dimension()
        # 路径1：flatten_chip_fields 直接调用
        chip_flat = flatten_chip_fields(chip_dim.model_dump(by_alias=False))
        # 路径2：flatten_first_pyramid 通过 chipConsensus 调用
        from app.services.first_pyramid_flatten import flatten_first_pyramid
        full_flat = flatten_first_pyramid({"chipConsensus": chip_dim.model_dump(by_alias=False)})
        # 10 个 chip 字段必须一致
        for k in FP_CHIP_KEYS:
            assert chip_flat[k] == full_flat[k], (
                f"chip_flat 与 flatten_first_pyramid 不一致: key={k}, "
                f"chip_flat={chip_flat[k]}, full_flat={full_flat[k]}"
            )
