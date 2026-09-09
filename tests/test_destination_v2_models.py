"""Home V2 destination read-model tests."""

from __future__ import annotations

from app.services import deal_manager, performance_manager, stats_merge


def test_performance_manager_surfaces_only_supplied_metrics() -> None:
    view = performance_manager.build_view(
        rows=[
            stats_merge.StatsRow(
                source="instagram",
                title="post with reach",
                timestamp="2026-09-08T10:00:00Z",
                permalink="https://www.instagram.com/p/real",
                metrics={"reach": 1200, "likes": 90},
                notes=None,
            )
        ],
        active_platform="instagram",
        platform_label="Instagram",
        instagram_status=stats_merge.IG_STATUS_OK,
    )

    card = view["cards"][0]
    assert card["primary_metric"]["label"] == "reach"
    assert card["primary_metric"]["formatted"] == "1,200"
    labels = [metric["label"] for metric in card["metrics"]]
    assert labels == ["reach", "likes"]
    assert "not a reliable pattern" in view["summary"]["read"]


def test_performance_manager_calls_out_real_standout_without_template_filler() -> None:
    view = performance_manager.build_view(
        rows=[
            stats_merge.StatsRow(
                source="instagram",
                title="winner",
                timestamp="2026-09-08T10:00:00Z",
                permalink=None,
                metrics={"engagement": 100},
                notes=None,
            ),
            stats_merge.StatsRow(
                source="instagram",
                title="baseline one",
                timestamp="2026-09-07T10:00:00Z",
                permalink=None,
                metrics={"engagement": 40},
                notes=None,
            ),
            stats_merge.StatsRow(
                source="instagram",
                title="baseline two",
                timestamp="2026-09-06T10:00:00Z",
                permalink=None,
                metrics={"engagement": 30},
                notes=None,
            ),
        ],
        active_platform="instagram",
        platform_label="Instagram",
        instagram_status=stats_merge.IG_STATUS_OK,
    )

    assert "winner" in view["summary"]["read"]
    combined = " ".join(
        [view["summary"]["read"], view["summary"]["evidence"], view["summary"]["action"]]
    ).lower()
    assert "comments are the signal" not in combined
    assert "sharpen the caption" not in combined
    assert "watch this" not in combined


def test_deal_manager_formats_terms_without_dumping_raw_json() -> None:
    deal = deal_manager.detail_view(
        {
            "id": "deal-1",
            "brand_name": "Acme",
            "stage": "waiting_on_terms",
            "agreed_amount_cents": None,
            "paid_amount_cents": None,
            "deliverables": ["1 reel", "2 stories"],
            "usage_rights": {"paid_usage": "not approved"},
            "exclusivity_notes": "No category exclusivity yet.",
            "platform": "instagram",
            "deadline": "2026-10-01",
            "payment_terms": "net 30",
        },
        [{"stated_amount_cents": 250000}],
    )

    assert deal["current_offer"] == "$2,500"
    assert deal["recommendation"].startswith("Clarify deliverables")
    term_values = {item["label"]: item["value"] for item in deal["term_items"]}
    assert term_values["deliverables"] == "1 reel, 2 stories"
    assert term_values["usage"] == "paid usage: not approved"
    assert term_values["payment"] == "net 30"
