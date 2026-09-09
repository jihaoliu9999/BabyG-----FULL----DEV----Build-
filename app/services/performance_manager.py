"""Manager-shaped performance read model for the creator destination page.

This module is intentionally read-only. It takes the real rows already
assembled by stats_merge and turns them into a concise hierarchy:
BabyG's read, what evidence supports it, the recommendation, then the
underlying posts/snapshots.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from app.services import stats_merge

METRIC_LABELS: dict[str, str] = {
    "engagement_rate": "engagement",
    "follower_delta": "followers",
    "deal_count": "brand deals",
    "deal_value": "deal value",
    "likes": "likes",
    "comments": "comments",
    "reach": "reach",
    "impressions": "impressions",
    "views": "views",
    "video_views": "video views",
    "plays": "plays",
    "shares": "shares",
    "saved": "saves",
    "saves": "saves",
    "engagement": "engagement",
}

_PRIMARY_METRICS: tuple[str, ...] = (
    "engagement",
    "views",
    "video_views",
    "plays",
    "reach",
    "impressions",
    "likes",
    "comments",
    "shares",
    "saved",
    "saves",
    "engagement_rate",
    "follower_delta",
    "deal_count",
    "deal_value",
)


def build_view(
    *,
    rows: list[stats_merge.StatsRow],
    active_platform: str,
    platform_label: str,
    instagram_status: str,
) -> dict[str, Any]:
    cards = [_card_for_row(row, sample_rows=rows) for row in rows]
    cards.sort(key=lambda card: (card["timestamp"] is not None, card["timestamp"] or ""), reverse=True)
    summary = _summary(cards, active_platform=active_platform, platform_label=platform_label)
    return {
        "cards": cards,
        "summary": summary,
        "metric_labels": METRIC_LABELS,
        "status_label": _status_label(active_platform, platform_label, instagram_status),
        "footer": _footer_copy(active_platform, platform_label, instagram_status),
        "show_error": active_platform == "instagram" and instagram_status == stats_merge.IG_STATUS_ERROR,
    }


def _card_for_row(
    row: stats_merge.StatsRow, *, sample_rows: list[stats_merge.StatsRow]
) -> dict[str, Any]:
    metrics = _metric_items(row.metrics)
    primary = _primary_metric(row.metrics)
    same_metric_values = [
        float(other.metrics[primary["key"]])
        for other in sample_rows
        if primary
        and isinstance(other.metrics.get(primary["key"]), int | float)
        and not isinstance(other.metrics.get(primary["key"]), bool)
    ]
    interpretation = _interpretation(row, primary=primary, values=same_metric_values)
    return {
        "source": row.source,
        "platform_label": "Instagram" if row.source == stats_merge.SOURCE_INSTAGRAM else "manual",
        "title": row.title,
        "timestamp": row.timestamp,
        "permalink": row.permalink,
        "metrics": metrics,
        "primary_metric": primary,
        "interpretation": interpretation,
        "recommendation": _recommendation(row, primary=primary, values=same_metric_values),
        "notes": row.notes,
    }


def _summary(
    cards: list[dict[str, Any]],
    *,
    active_platform: str,
    platform_label: str,
) -> dict[str, str]:
    if active_platform != "instagram":
        return {
            "label": f"{platform_label} not connected",
            "read": f"BabyG does not have a real {platform_label} performance feed yet.",
            "evidence": "Instagram is the only live social analytics destination connected in this build.",
            "action": "Use Instagram performance or connect supported sources from settings when available.",
        }
    if not cards:
        return {
            "label": "no read yet",
            "read": "BabyG does not have enough real Instagram performance evidence yet.",
            "evidence": "No connected Instagram posts or saved performance snapshots are available for this view.",
            "action": "Reconnect Instagram if needed, then let BabyG collect real post metrics before changing strategy.",
        }

    measurable = [card for card in cards if card.get("primary_metric")]
    if len(measurable) < 3:
        return {
            "label": "early evidence",
            "read": "BabyG has an early read, not a reliable pattern yet.",
            "evidence": f"{len(measurable)} real item{'s' if len(measurable) != 1 else ''} currently have usable metrics.",
            "action": "Treat this as a baseline and avoid changing strategy until more connected posts are measured.",
        }

    standout = _standout_card(measurable)
    if standout:
        metric = standout["primary_metric"]
        return {
            "label": "what changed",
            "read": f"{standout['title']} is the strongest recent signal BabyG can verify.",
            "evidence": f"It leads the current sample on {metric['label']} with {metric['formatted']}.",
            "action": "Open the post, identify the visible hook or format, then repeat only the element the evidence supports.",
        }

    return {
        "label": "steady sample",
        "read": "BabyG can read the recent posts, but there is no clear standout pattern yet.",
        "evidence": f"{len(measurable)} real items have measurable results, and the spread is not strong enough to call a winner.",
        "action": "Keep collecting posts and compare the next result against this baseline before acting.",
    }


def _standout_card(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    comparable: list[tuple[float, dict[str, Any]]] = []
    for card in cards:
        metric = card.get("primary_metric") or {}
        value = metric.get("raw")
        if isinstance(value, int | float) and not isinstance(value, bool):
            comparable.append((float(value), card))
    if len(comparable) < 3:
        return None
    comparable.sort(key=lambda pair: pair[0], reverse=True)
    top_value, top_card = comparable[0]
    baseline = median([value for value, _card in comparable])
    if baseline <= 0:
        return top_card if top_value > 0 else None
    return top_card if top_value >= baseline * 1.35 else None


def _metric_items(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in _PRIMARY_METRICS:
        if key not in metrics:
            continue
        value = metrics.get(key)
        if value is None:
            continue
        out.append(
            {
                "key": key,
                "label": METRIC_LABELS.get(key, key.replace("_", " ")),
                "raw": value,
                "formatted": _format_metric(key, value),
            }
        )
    for key, value in metrics.items():
        if key in _PRIMARY_METRICS or value is None:
            continue
        out.append(
            {
                "key": key,
                "label": METRIC_LABELS.get(key, key.replace("_", " ")),
                "raw": value,
                "formatted": _format_metric(key, value),
            }
        )
    return out


def _primary_metric(metrics: dict[str, Any]) -> dict[str, Any] | None:
    items = _metric_items(metrics)
    return items[0] if items else None


def _interpretation(
    row: stats_merge.StatsRow,
    *,
    primary: dict[str, Any] | None,
    values: list[float],
) -> str:
    if not primary:
        if row.source == stats_merge.SOURCE_INSTAGRAM:
            return "real Instagram post; this provider response did not include usable metrics."
        return "saved performance snapshot without comparable metrics."
    if row.source != stats_merge.SOURCE_INSTAGRAM:
        return "manual snapshot; useful context, but not a live post-level signal."
    if len(values) < 3:
        return "measured result; sample is still too small for a pattern."
    value = primary.get("raw")
    if isinstance(value, int | float) and not isinstance(value, bool):
        base = median(values)
        if base > 0 and float(value) >= base * 1.35:
            return f"above the recent sample on {primary['label']}."
    return "real post-level evidence; not enough separation to call a pattern."


def _recommendation(
    row: stats_merge.StatsRow,
    *,
    primary: dict[str, Any] | None,
    values: list[float],
) -> str:
    if not primary:
        return "Use the source post and future metrics before making a content decision."
    if row.source != stats_merge.SOURCE_INSTAGRAM:
        return "Use this as historical context, not as a standalone next move."
    if len(values) < 3:
        return "Keep it in the baseline and wait for more real posts before changing strategy."
    value = primary.get("raw")
    if isinstance(value, int | float) and not isinstance(value, bool):
        base = median(values)
        if base > 0 and float(value) >= base * 1.35:
            return "Repeat the visible format or hook once, then compare the next post against this result."
    return "Do not force a pattern yet; compare the next related post before acting."


def _format_metric(key: str, value: Any) -> str:
    if key == "engagement_rate":
        return f"{value}%"
    if key == "follower_delta" and isinstance(value, int | float) and value > 0:
        return f"+{value:g}"
    if key == "deal_value":
        return f"${value}"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _status_label(active_platform: str, platform_label: str, instagram_status: str) -> str:
    if active_platform != "instagram":
        return f"{platform_label} not connected"
    if instagram_status == stats_merge.IG_STATUS_OK:
        return "instagram synced"
    if instagram_status == stats_merge.IG_STATUS_ERROR:
        return "instagram paused"
    if instagram_status == stats_merge.IG_STATUS_NOT_CONNECTED:
        return "instagram not connected"
    return "manual view"


def _footer_copy(active_platform: str, platform_label: str, instagram_status: str) -> str:
    if active_platform != "instagram":
        return f"{platform_label} platform view · connection not available yet"
    if instagram_status == stats_merge.IG_STATUS_OK:
        return "manual + instagram · real post stats merged"
    if instagram_status == stats_merge.IG_STATUS_ERROR:
        return "manual entries · instagram live data unavailable"
    if instagram_status == stats_merge.IG_STATUS_NOT_CONNECTED:
        return "manual entries · connect instagram for live post stats"
    return "manual entries for now · auto-sync requires platform api access"
