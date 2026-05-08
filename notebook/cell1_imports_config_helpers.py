# ============================================================
# CELL 1 — Imports, Config, Shared Helpers, GIS Connection
# ============================================================

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import List, Optional, Set, Tuple

from arcgis.gis import GIS
from arcgis.features import FeatureLayer
from arcgis.geometry import Geometry
from arcgis.geometry.filters import intersects

import json
import math
import os
import re
import traceback
import warnings
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from PIL import Image, ImageDraw, ImageFont


# -------------------------
# PROJECT TAGGING CONFIG
# -------------------------

TAG_ACTIVE = "Active Projects"
TAG_INACTIVE = "Inactive Projects"
TAG_DORMANT = "Dormant Projects"
TAG_COMPLETED = "Completed Projects"

STATUS_TAGS = {TAG_ACTIVE, TAG_INACTIVE, TAG_DORMANT}

COLLECTION_DATE_FIELD = "Collection_Date"
CREATOR_FIELD = "Creator"
OFFICE_CREATORS = {"Landscapes_Unlimited", "cpeterson_LandscapesM4"}

ACTIVE_HOURS = 168    # 7 days
INACTIVE_HOURS = 720  # 30 days

EXCLUDE_WORKFORCE = True

NOTIFY_GROUP_ID = "f0f8bc9e0a73486a9d1bc79cd673cfc3"


# -------------------------
# DATA HEALTH CONFIG
# -------------------------

TARGET_FOLDER = "Data Health Reports"
IMG_WIDTH = 480
OUT_DIR = "/tmp"

ENABLE_SHEETS_LOGGING = True
SHEETS_ENDPOINT = "https://script.google.com/macros/s/AKfycbzzbVO_yJ84Zv_4RS3JYlkXhA4xTIGlHq4ghzbJBs4v-IlnRKjSb1Y6N1is6G-5CN9Juw/exec"
SHEETS_TIMEOUT_SECONDS = 30

HEAD_LAYER_SPECS = [
    ("Toro Head", "HeadType"),
    ("Hunter Head", "HeadType"),
    ("Rain Bird Head", "Head_Type"),
]

IRRIGATION_POINT_SPECS = [
    ("Quick Couplers", "Quick Coupler", "QCV_Size"),
    ("Isolation Valves", "Isolation Valve", "Size"),
    ("Lateral Valves", "Lateral Valve", "Size"),
]

IRRIGATION_PIPE_SPECS = [
    ("Lateral Pipe", "Lateral", "Size"),
    ("Mainline", "Mainline", "Pipe_Size"),
]

DRAINAGE_POINT_SPECS = [
    ("Basins", "Basin", "Size"),
]

DRAINAGE_PIPE_SPECS = [
    ("Perf Drainage", "Perf Drainage Pipe", "Size"),
    ("Solid Drainage", "Solid Drainage Pipe", "Size"),
]

PIPE_LENGTH_FIELD = "Length"
AREA_FIELD_CANDIDATES = ["Shape__Area", "Shape_Area", "Area", "SHAPE_Area"]
HOLE_FIELD = "HoleNo"

GOLF_LAYER_SPECS = [
    ("Greens", "Green"),
    ("Tees", "Tee"),
    ("Bunkers", "Bunker"),
    ("Fairways", "Fairway"),
]
FAIRWAY_LAYER_TITLE = "Fairway"

WEIGHTS = {
    "Heads": 1.0,
    "Quick Couplers": 1.0,
    "Isolation Valves": 5600 / 900,
    "Lateral Valves": 1300 / 900,
    "Lateral Pipe": 5 / 900,
    "Mainline": 40 / 900,
    "Basins": 600 / 900,
    "Perf Drainage": 13 / 900,
    "Solid Drainage": 20 / 900,
    "Greens": 8 / 900,
    "Tees": 1 / 900,
    "Bunkers": 6 / 900,
    "Fairways": 8 / 900,
}


# -------------------------
# SHARED HELPER STRUCTURES
# -------------------------

@dataclass
class MapResult:
    item_id: str
    title: str
    prev_status: Optional[str]
    new_status: Optional[str]
    last_field_activity_utc: Optional[datetime]


# -------------------------
# SHARED UTILITY FUNCTIONS
# -------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_epoch_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def format_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "N/A"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def pct(numer, denom):
    if not denom:
        return 0.0
    return (numer / denom) * 100.0


def fmt_ft(x):
    try:
        return f"{float(x):,.0f} ft"
    except Exception:
        return "0 ft"


def fmt_num(x):
    try:
        return f"{float(x):,.1f}"
    except Exception:
        return "0.0"


def safe_filename(s: str, max_len=140):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:max_len]


def status_from_tags(tags: List[str]) -> Optional[str]:
    for t in (TAG_ACTIVE, TAG_INACTIVE, TAG_DORMANT):
        if t in tags:
            return t
    return None


def compute_new_status(last_activity_utc: Optional[datetime], now_utc: datetime) -> Optional[str]:
    if last_activity_utc is None:
        return None
    age_hours = (now_utc - last_activity_utc).total_seconds() / 3600.0
    if age_hours <= ACTIVE_HOURS:
        return TAG_ACTIVE
    if age_hours <= INACTIVE_HOURS:
        return TAG_INACTIVE
    return TAG_DORMANT


def build_webmap_search_query(org_id: str) -> str:
    q = f'orgid:{org_id} AND type:"Web Map" AND NOT typekeywords:FieldMapsDisabled'
    if EXCLUDE_WORKFORCE:
        q += ' AND NOT typekeywords:"Workforce Project" AND NOT typekeywords:"Workforce Dispatcher" AND NOT typekeywords:"Workforce Worker"'
    return q


def search_all_items(gis: GIS, query: str, max_items: int = 10000) -> list:
    return gis.content.search(query=query, max_items=max_items)


# -------------------------
# SHARED FEATURE LAYER HELPERS
# -------------------------

def find_layer_url(webmap_data: dict, layer_title: str):
    for lyr in (webmap_data.get("operationalLayers", []) or []):
        if lyr.get("title") == layer_title and lyr.get("url"):
            return lyr["url"]
    return None


def safe_fl(url: str):
    if not url:
        return None
    try:
        return FeatureLayer(url, gis=gis)
    except Exception:
        return None


def layer_has_fields(layer: FeatureLayer, required: Set[str]) -> bool:
    try:
        fld_names = {f["name"] for f in layer.properties.fields}
        return required.issubset(fld_names)
    except Exception:
        return False


def field_exists(layer: FeatureLayer, field_name: str) -> bool:
    if not layer or not field_name:
        return False
    return layer_has_fields(layer, {field_name})


def get_area_field(layer: FeatureLayer):
    for fld in AREA_FIELD_CANDIDATES:
        if field_exists(layer, fld):
            return fld
    return None


def get_objectid_field(layer: FeatureLayer):
    try:
        return layer.properties.objectIdField
    except Exception:
        return None


def is_null_or_empty_sql(field: str) -> str:
    return f"({field} IS NULL OR {field} = '')"


def count_where(layer: FeatureLayer, where: str) -> int:
    if not layer:
        return 0
    try:
        return int(layer.query(where=where, return_count_only=True))
    except Exception:
        return 0


def sum_field(layer: FeatureLayer, field: str, where: str = "1=1") -> float:
    if not layer or not field:
        return 0.0
    try:
        stat = [{"statisticType": "sum", "onStatisticField": field, "outStatisticFieldName": "s"}]
        fs = layer.query(where=where, out_statistics=stat, return_geometry=False)
        feats = getattr(fs, "features", []) or []
        if not feats:
            return 0.0
        v = feats[0].attributes.get("s", 0) or 0
        return float(v)
    except Exception:
        return 0.0


def fetch_features(layer: FeatureLayer, where: str = "1=1", out_fields: str = "*", return_geometry: bool = True):
    if not layer:
        return []
    try:
        fs = layer.query(where=where, out_fields=out_fields, return_geometry=return_geometry)
        return getattr(fs, "features", []) or []
    except Exception:
        return []


def get_shape_area_from_feature(feature, area_field: str) -> float:
    if not feature or not area_field:
        return 0.0
    try:
        return float(feature.attributes.get(area_field) or 0.0)
    except Exception:
        return 0.0


def normalize_date_value(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return None


def geometry_intersects(g1, g2) -> bool:
    try:
        return bool(Geometry(g1).intersects(Geometry(g2)))
    except Exception:
        return False


def group_overlapping_polygons(features):
    n = len(features)
    if n < 2:
        return []
    parents = list(range(n))

    def find(x):
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parents[rb] = ra

    for i in range(n):
        gi = features[i].geometry
        if not gi:
            continue
        for j in range(i + 1, n):
            gj = features[j].geometry
            if not gj:
                continue
            if geometry_intersects(gi, gj):
                union(i, j)

    groups = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)

    return [idxs for idxs in groups.values() if len(idxs) > 1]


def max_collection_date_ms(layer: FeatureLayer) -> Optional[int]:
    office_list = "', '".join(sorted(OFFICE_CREATORS))
    where = (
        f"{CREATOR_FIELD} NOT IN ('{office_list}') "
        f"AND {COLLECTION_DATE_FIELD} IS NOT NULL"
    )
    out_statistics = [{
        "statisticType": "MAX",
        "onStatisticField": COLLECTION_DATE_FIELD,
        "outStatisticFieldName": "max_cd"
    }]
    fs = layer.query(where=where, out_statistics=out_statistics, return_geometry=False)
    if not fs or not fs.features:
        return None
    val = fs.features[0].attributes.get("max_cd")
    return int(val) if val is not None else None


def get_last_field_activity_for_webmap(webmap_item) -> Tuple[Optional[datetime], bool]:
    required_fields = {COLLECTION_DATE_FIELD, CREATOR_FIELD}
    best_ms: Optional[int] = None
    found_any_layer = False

    data = webmap_item.get_data() or {}
    layers = data.get("operationalLayers", [])

    for lyr in layers:
        url = lyr.get("url")
        if not url:
            continue
        try:
            fl = FeatureLayer(url, gis=gis)
        except Exception:
            continue
        if not layer_has_fields(fl, required_fields):
            continue
        found_any_layer = True
        try:
            layer_max = max_collection_date_ms(fl)
        except Exception:
            layer_max = None
        if layer_max is None:
            continue
        if best_ms is None or layer_max > best_ms:
            best_ms = layer_max

    if not found_any_layer:
        return None, False
    if best_ms is None:
        return None, True
    return ms_epoch_to_dt(best_ms), True


def set_status_tags(item, new_status: Optional[str]) -> None:
    tags = list(item.tags) if item.tags else []
    tags = [t for t in tags if t not in STATUS_TAGS]
    if new_status in STATUS_TAGS:
        tags.append(new_status)
    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    item.update(item_properties={"tags": deduped})


def distinct_creators_for_layer(layer: FeatureLayer) -> set:
    if not layer:
        return set()
    try:
        fs = layer.query(where="1=1", out_fields="Creator", return_geometry=False, return_distinct_values=True)
        creators = set()
        for f in (getattr(fs, "features", []) or []):
            c = (f.attributes.get("Creator") or "").strip()
            if c:
                creators.add(c)
        return creators
    except Exception:
        try:
            fs = layer.query(where="1=1", out_fields="Creator", return_geometry=False)
            creators = set()
            for f in (getattr(fs, "features", []) or []):
                c = (f.attributes.get("Creator") or "").strip()
                if c:
                    creators.add(c)
            return creators
        except Exception:
            return set()


# -------------------------
# NOTIFICATION HELPER
# -------------------------

def notify_group(subject: str, message: str) -> None:
    group = gis.groups.get(NOTIFY_GROUP_ID)
    if group is None:
        raise RuntimeError(f"Group not found or not accessible: {NOTIFY_GROUP_ID}")
    members = group.get_members()
    usernames = members.get("users", [])
    if not usernames:
        raise RuntimeError("No users found in the notification group.")
    group.notify(users=usernames, subject=subject, message=message)


# -------------------------
# GOOGLE SHEETS HELPER
# -------------------------

def post_to_google_sheets(payload: dict):
    if not ENABLE_SHEETS_LOGGING:
        return False, "Sheets logging disabled"
    if not SHEETS_ENDPOINT or "YOUR_DEPLOYMENT_ID" in SHEETS_ENDPOINT:
        return False, "SHEETS_ENDPOINT not configured"
    body = json.dumps(payload).encode("utf-8")
    req = Request(SHEETS_ENDPOINT, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=SHEETS_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return True, text
    except HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="replace")
        except Exception:
            msg = str(e)
        return False, f"HTTPError {e.code}: {msg}"
    except URLError as e:
        return False, f"URLError: {e}"
    except Exception as e:
        return False, f"Error: {e}"


# -------------------------
# PILLOW FONT HELPERS
# -------------------------

def load_font_prefer(name="DejaVuSans", size=12, bold=False):
    fam = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/{fam}",
        f"/usr/share/fonts/{fam}",
        fam,
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_TITLE = load_font_prefer(size=16, bold=True)
FONT_H3    = load_font_prefer(size=12, bold=True)
FONT_BODY  = load_font_prefer(size=11)
FONT_SMALL = load_font_prefer(size=9)


def measure_text(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_progress_bar(draw, x, y, width, height, ratio):
    radius = max(2, int(height / 2))
    back_color = (230, 230, 230)
    stroke = (204, 204, 204)
    fill_color = (46, 125, 50) if ratio >= 0.999999 else ((249, 168, 37) if ratio >= 0.95 else (198, 40, 40))
    draw.rounded_rectangle([x, y, x + width, y + height], radius=radius, fill=back_color, outline=stroke)
    fill_w = int(width * max(0.0, min(1.0, ratio)))
    if fill_w > 0:
        draw.rounded_rectangle([x, y, x + fill_w, y + height], radius=radius, fill=fill_color, outline=None)


def draw_metric_bar(draw, x0, x1, y, label, stat, ratio, bar_height=10, font=FONT_BODY, text_color=(30, 30, 30)):
    draw.text((x0, y), label, font=font, fill=text_color)
    stat_w, _ = measure_text(draw, stat, font)
    draw.text((x1 - stat_w, y), stat, font=font, fill=text_color)
    y += 16
    draw_progress_bar(draw, x0, y, x1 - x0, bar_height, ratio)
    y += bar_height + 4
    return y


def draw_bullets(draw, x0, y, lines, font=FONT_BODY, color=(30, 30, 30), indent=8):
    for line in lines:
        bullet = f"• {line}"
        draw.text((x0 + indent, y), bullet, font=font, fill=color)
        _, h = measure_text(draw, bullet, font)
        y += h + 4
    return y


# -------------------------
# COLLECTOR SCORING HELPERS
# -------------------------

def add_points(bucket, collector, category, earned, possible):
    collector = (collector or "Unknown").strip() or "Unknown"
    key = (collector, category)
    if key not in bucket:
        bucket[key] = {
            "collector": collector,
            "category": category,
            "earned": 0.0,
            "possible": 0.0,
        }
    bucket[key]["earned"] += float(earned or 0)
    bucket[key]["possible"] += float(possible or 0)


def collector_field_scores(
    layer,
    collector_scores,
    category,
    value_field=None,
    length_field=None,
    weight=1.0
):
    if not layer or not field_exists(layer, CREATOR_FIELD):
        return
    if value_field and not field_exists(layer, value_field):
        return
    if length_field and not field_exists(layer, length_field):
        return

    fields = [CREATOR_FIELD]
    if value_field:
        fields.append(value_field)
    if length_field:
        fields.append(length_field)

    try:
        fs = layer.query(
            where="1=1",
            out_fields=",".join(fields),
            return_geometry=False
        )
    except Exception:
        return

    for f in (getattr(fs, "features", []) or []):
        a = f.attributes or {}
        collector = a.get(CREATOR_FIELD)

        if length_field:
            possible_base = float(a.get(length_field) or 0)
            complete = a.get(value_field) not in (None, "")
            earned_base = possible_base if complete else 0.0
        else:
            possible_base = 1.0
            complete = a.get(value_field) not in (None, "")
            earned_base = 1.0 if complete else 0.0

        add_points(
            collector_scores,
            collector,
            category,
            earned_base * weight,
            possible_base * weight
        )


def post_collector_scores_to_sheets(
    as_of_date,
    generated_text,
    base_title,
    webmap_item_id,
    collector_scores
):
    rows = list(collector_scores.values())
    if not rows:
        return

    def build_and_post(row):
        possible = float(row["possible"] or 0)
        payload = {
            "record_key": (
                f"{webmap_item_id}_"
                f"{as_of_date.isoformat()}_"
                f"{row['collector']}_"
                f"{row['category']}"
            ),
            "row_type":        "collector_score",
            "run_date":        as_of_date.isoformat(),
            "run_timestamp":   generated_text,
            "project_ref":     base_title,          # not project_name — keeps collector rows out of dashboard project list
            "project_item_id": webmap_item_id,
            "collector":       row["collector"],
            "score_category":  row["category"],
            "points_earned":   round(row["earned"], 1),
            "points_possible": round(possible, 1),
            "confidence_pct":  round(pct(row["earned"], possible), 1),
        }
        return post_to_google_sheets(payload)

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(build_and_post, rows))

    ok_count = sum(1 for ok, _ in results if ok)
    fail_count = len(results) - ok_count
    print(
        f"  Collector Sheets: {ok_count} OK, {fail_count} failed "
        f"({len(rows)} rows)"
    )


# -------------------------
# DATA HEALTH HELPER FUNCTIONS
# -------------------------

def analyze_golf_layer(layer: FeatureLayer, group_label: str, collector_scores=None, weight=1.0):
    result = {
        "display_label": group_label,
        "feature_ok": 0,
        "feature_total": 0,
        "bullet_lines": [],
        "earned_units": 0.0,
        "possible_units": 0.0,
        "total_sqft": 0.0,
        "correct_sqft": 0.0,
        "incorrect_sqft": 0.0,
        "missing_hole_count": 0,
        "intersecting_count": 0,
    }

    if not layer:
        return result

    area_field = get_area_field(layer)
    oid_field = get_objectid_field(layer)

    if not area_field or not oid_field:
        return result

    has_creator = (
        collector_scores is not None
        and field_exists(layer, CREATOR_FIELD)
    )
    creator_clause = f",{CREATOR_FIELD}" if has_creator else ""

    features = fetch_features(
        layer,
        where="1=1",
        out_fields=f"{oid_field},{HOLE_FIELD},{COLLECTION_DATE_FIELD},{area_field}{creator_clause}",
        return_geometry=True,
    )

    if not features:
        return result

    total_area = 0.0
    missing_hole_count = 0
    superseded_oids = set()

    result["feature_total"] = len(features)

    for f in features:
        total_area += get_shape_area_from_feature(f, area_field)
        hv = f.attributes.get(HOLE_FIELD)
        if hv in (None, ""):
            missing_hole_count += 1

    overlap_groups = group_overlapping_polygons(features)
    intersecting_feature_count = 0

    if overlap_groups:
        intersecting_feature_count = sum(len(g) for g in overlap_groups)

        for idxs in overlap_groups:
            newest_idx = None
            newest_dt = -math.inf
            newest_oid = None

            for idx in idxs:
                f = features[idx]
                dtv = normalize_date_value(f.attributes.get(COLLECTION_DATE_FIELD))
                oidv = f.attributes.get(oid_field)
                compare_dt = dtv if dtv is not None else -math.inf

                if (
                    compare_dt > newest_dt
                    or (
                        compare_dt == newest_dt
                        and (newest_oid is None or oidv > newest_oid)
                    )
                ):
                    newest_dt = compare_dt
                    newest_idx = idx
                    newest_oid = oidv

            for idx in idxs:
                if idx != newest_idx:
                    superseded_oids.add(features[idx].attributes.get(oid_field))

    earned_area = 0.0
    feature_ok = 0

    for f in features:
        oidv = f.attributes.get(oid_field)
        area = get_shape_area_from_feature(f, area_field)
        has_hole = f.attributes.get(HOLE_FIELD) not in (None, "")
        not_superseded = oidv not in superseded_oids

        if has_hole and not_superseded:
            earned_area += area
            feature_ok += 1

        if has_creator:
            collector = f.attributes.get(CREATOR_FIELD)
            earned_pts = area if (has_hole and not_superseded) else 0.0
            add_points(
                collector_scores,
                collector,
                group_label,
                earned_pts * weight,
                area * weight,
            )

    result["feature_ok"] = feature_ok
    result["total_sqft"] = total_area
    result["correct_sqft"] = earned_area
    result["incorrect_sqft"] = max(0.0, total_area - earned_area)
    result["possible_units"] = total_area * weight
    result["earned_units"] = earned_area * weight
    result["missing_hole_count"] = missing_hole_count
    result["intersecting_count"] = intersecting_feature_count

    if missing_hole_count > 0:
        singular = group_label[:-1] if group_label.endswith("s") else group_label
        suffix = "s" if missing_hole_count != 1 else ""
        result["bullet_lines"].append(
            f"{missing_hole_count} {singular}{suffix} missing Hole Number"
        )

    if intersecting_feature_count > 0:
        singular = group_label[:-1] if group_label.endswith("s") else group_label
        suffix = "s" if intersecting_feature_count != 1 else ""
        result["bullet_lines"].append(
            f"{intersecting_feature_count} {singular}{suffix} intersecting"
        )

    return result


# -------------------------
# ITEM BUILDER HELPERS
# -------------------------

def build_count_item(label: str, total: int, filled: int) -> dict:
    """Display-ready dict for a count-based metric row."""
    return {
        "type": "count",
        "label": label,
        "total": int(total),
        "filled": int(filled),
        "pct": pct(filled, total),
    }


def build_length_item(label: str, total_len: float, ok_len: float) -> dict:
    """Display-ready dict for a pipe/length-based metric row."""
    return {
        "type": "length",
        "label": label,
        "total": float(total_len),
        "filled": float(ok_len),
        "pct": pct(ok_len, total_len),
    }


def build_golf_item(group_label: str, feature_ok: int, feature_total: int, bullet_lines: list) -> dict:
    """Display-ready dict for a golf-layer metric row."""
    return {
        "type": "golf",
        "label": group_label,
        "total": int(feature_total),
        "filled": int(feature_ok),
        "pct": pct(feature_ok, feature_total),
        "bullets": list(bullet_lines or []),
    }


# -------------------------
# IMAGE RENDERER
# -------------------------

def render_full_report_image(metrics: dict, out_path: str, title_base: str, width_px=IMG_WIDTH):
    PAD = 14
    INNER_W = width_px - PAD * 2
    bg = (255, 255, 255)
    TEXT = (30, 30, 30)
    MUTED = (110, 110, 110)
    LINE = (225, 225, 225)

    tmp_h = 12000
    img = Image.new("RGB", (width_px, tmp_h), bg)
    draw = ImageDraw.Draw(img)
    x0, x1, y = PAD, width_px - PAD, PAD

    # Title
    heading = f"{title_base} Data Health Report"
    draw.text((x0, y), heading, font=FONT_TITLE, fill=TEXT)
    _, th = measure_text(draw, heading, FONT_TITLE)
    y += th + 6

    # As-of date
    ao = metrics.get("as_of_week")
    ao_text = ao.strftime("%B %d, %Y") if isinstance(ao, (datetime, date)) else str(ao or "")
    draw.text((x0, y), f"As of {ao_text}", font=FONT_SMALL, fill=MUTED)
    _, sh = measure_text(draw, f"As of {ao_text}", FONT_SMALL)
    y += sh + 10

    # Overall confidence score bar
    total_earned = float(metrics.get("total_earned_points") or 0)
    total_possible = float(metrics.get("total_possible_points") or 0)
    if total_possible > 0:
        draw.line((x0, y, x1, y), fill=LINE, width=1)
        y += 8
        overall_pct = pct(total_earned, total_possible)
        score_lbl = f"Overall Confidence: {overall_pct:.1f}%"
        draw.text((x0, y), score_lbl, font=FONT_H3, fill=TEXT)
        _, h = measure_text(draw, score_lbl, FONT_H3)
        y += h + 6
        draw_progress_bar(draw, x0, y, INNER_W, 12, overall_pct / 100.0)
        y += 18 + 8

    def render_section(section_title: str, items: list):
        nonlocal y
        if not items:
            return
        draw.line((x0, y, x1, y), fill=LINE, width=1)
        y += 8
        draw.text((x0, y), section_title, font=FONT_H3, fill=TEXT)
        _, h = measure_text(draw, section_title, FONT_H3)
        y += h + 6
        for it in items:
            lbl    = it.get("label", "")
            total  = it.get("total", 0) or 0
            filled = it.get("filled", 0) or 0
            ipct   = it.get("pct", 0.0)
            if it.get("type") == "length":
                stat = f"{fmt_ft(filled)} / {fmt_ft(total)} ({ipct:.1f}%)"
            else:
                stat = f"{int(filled):,} / {int(total):,} ({ipct:.1f}%)"
            y = draw_metric_bar(draw, x0, x1, y, lbl, stat, ipct / 100.0)
            bullets = it.get("bullets") or []
            if bullets:
                y = draw_bullets(draw, x0, y, bullets, font=FONT_SMALL, color=(176, 0, 32))
            y += 4
        y += 4

    render_section("Irrigation",    metrics.get("irrigation_items") or [])
    render_section("Drainage",      metrics.get("drainage_items")   or [])
    render_section("Golf Features", metrics.get("golf_items")       or [])

    # Collectors
    collectors = metrics.get("collectors") or []
    draw.line((x0, y, x1, y), fill=LINE, width=1)
    y += 8
    draw.text((x0, y), "GPS Collectors", font=FONT_H3, fill=TEXT)
    _, h = measure_text(draw, "GPS Collectors", FONT_H3)
    y += h + 6
    if collectors:
        for c in collectors:
            bullet = f"• {c}"
            draw.text((x0, y), bullet, font=FONT_BODY, fill=TEXT)
            _, ch = measure_text(draw, bullet, FONT_BODY)
            y += ch + 6
    else:
        draw.text((x0, y), "No collectors found.", font=FONT_BODY, fill=MUTED)
        _, ch = measure_text(draw, "No collectors found.", FONT_BODY)
        y += ch + 8

    # Footer
    y += 6
    gen = metrics.get("generated_text") or datetime.now().strftime("%Y-%m-%d %I:%M %p")
    footer = f"Generated: {gen}"
    draw.text((x0, y), footer, font=FONT_SMALL, fill=MUTED)
    _, fh = measure_text(draw, footer, FONT_SMALL)
    y += fh + PAD

    final_h = max(220, min(tmp_h, int(y)))
    img = img.crop((0, 0, width_px, final_h))
    img.save(out_path, "JPEG", quality=90, optimize=True)


# -------------------------
# AGOL FOLDER & UPSERT
# -------------------------

def ensure_folder_for_user(folder_title: str):
    user = gis.users.me
    for f in (user.folders or []):
        try:
            if isinstance(f, dict) and f.get("title") == folder_title:
                return folder_title
        except Exception:
            pass
        try:
            if getattr(f, "title", None) == folder_title:
                return folder_title
        except Exception:
            pass
    try:
        if hasattr(user, "create_folder"):
            user.create_folder(folder_title)
    except Exception:
        pass
    return folder_title


def upsert_jpeg_item(jpg_path: str, item_title: str, folder_title: str = TARGET_FOLDER):
    owner = gis.users.me.username
    matches = gis.content.search(
        query=f'title:"{item_title}" AND owner:{owner}',
        max_items=20
    )
    img_item = None
    pdf_items = []
    for it in matches:
        t = (it.type or "").lower()
        if "pdf" in t:
            pdf_items.append(it)
        elif "image" in t or "jpeg" in t or "jpg" in t:
            img_item = it
            break

    if img_item:
        img_item.update(data=jpg_path)
        action = "updated"
    else:
        folder_title = ensure_folder_for_user(folder_title)
        props = {
            "title": item_title,
            "tags": TAG_ACTIVE,
            "snippet": "Auto-generated weekly data health report (image).",
        }
        try:
            img_item = gis.content.add(item_properties=props, data=jpg_path, folder=folder_title)
        except Exception:
            try:
                props_img = dict(props)
                props_img["type"] = "Image"
                img_item = gis.content.add(item_properties=props_img, data=jpg_path, folder=folder_title)
            except Exception:
                props_file = dict(props)
                props_file["type"] = "File"
                img_item = gis.content.add(item_properties=props_file, data=jpg_path, folder=folder_title)
        action = "created"

    if pdf_items:
        for p in pdf_items:
            try:
                p.delete()
                print(f"  Deleted old PDF item with same title: {p.title} | {p.id}")
            except Exception as e:
                print(f"  WARNING: Could not delete PDF item {p.id}: {e}")

    return img_item, action


def make_item_public(item):
    try:
        from arcgis.gis._impl._content_manager import SharingLevel
        item.sharing.sharing_level = SharingLevel.EVERYONE
        return "sharing_level"
    except Exception:
        pass
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="share is deprecated.*")
            item.share(everyone=True)
        return "share"
    except Exception as e:
        raise RuntimeError(f"Could not make item public: {e}")


# -------------------------
# CONNECT
# -------------------------

gis = GIS("home")
now = utc_now()
print("Logged in as:", gis.users.me.username)
print("Run time:", format_dt(now))
