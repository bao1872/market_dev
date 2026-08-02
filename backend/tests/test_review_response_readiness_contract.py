from __future__ import annotations

from app.schemas.review import ReviewMetricPayloadDTO


def test_review_metric_response_preserves_readiness_evidence() -> None:
    payload = ReviewMetricPayloadDTO.model_validate(
        {
            "rawValue": 0.52,
            "status": "insufficient_history",
            "readiness": {
                "raw_ready": True,
                "normalized_ready": False,
                "reason": "history insufficient: 12 < 60",
                "history_observations": 12,
                "min_required": 60,
            },
            "components": [
                {
                    "name": "scope_return_1d",
                    "rawValue": 0.01,
                    "fieldSource": "review_return_1d",
                    "denominator": 88,
                    "weight": 1.0,
                    "weightMode": "equal_weight",
                    "status": "insufficient_history",
                    "readiness": {
                        "raw_ready": True,
                        "normalized_ready": False,
                        "reason": "history insufficient: 12 < 60",
                    },
                }
            ],
        }
    )
    dumped = payload.model_dump()
    assert dumped["readiness"]["reason"] == "history insufficient: 12 < 60"
    assert dumped["components"][0]["denominator"] == 88
    assert dumped["components"][0]["fieldSource"] == "review_return_1d"
    assert dumped["components"][0]["weightMode"] == "equal_weight"
    assert dumped["components"][0]["readiness"]["normalized_ready"] is False
