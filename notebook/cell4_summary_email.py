# ============================================================
# CELL 4 — Summary Email
# Combines project tagging log + data health confidence scores
# into one HTML email sent via Group.notify
# Depends on: Cell 1 (gis, helpers, format_dt, notify_group),
#             Cell 2 (tagging_results, now),
#             Cell 3 (confidence_scores)
# ============================================================

def confidence_str(item_id: str) -> str:
    """Returns formatted confidence string if available, else empty string."""
    score = confidence_scores.get(item_id)
    if score is None:
        return ""
    return f" | Confidence: {score:.1f}%"


def project_line(r: MapResult) -> str:
    return (
        f"- {r.title} — "
        f"Last field: {format_dt(r.last_field_activity_utc)}"
        f"{confidence_str(r.item_id)}"
    )


def section_html(title: str, rows: list) -> str:
    if not rows:
        return ""
    lines = "\n".join(f"<li>{project_line(r)}</li>" for r in sorted(rows, key=lambda x: x.title.lower()))
    return f"<h3>{title} ({len(rows)})</h3><ul>{lines}</ul>"


# Sort tagging results into buckets
moved_to_active, stayed_active = [], []
moved_to_inactive, stayed_inactive = [], []
moved_to_dormant, stayed_dormant = [], []

for r in tagging_results:
    if r.new_status == TAG_ACTIVE:
        (stayed_active if r.prev_status == TAG_ACTIVE else moved_to_active).append(r)
    elif r.new_status == TAG_INACTIVE:
        (stayed_inactive if r.prev_status == TAG_INACTIVE else moved_to_inactive).append(r)
    elif r.new_status == TAG_DORMANT:
        (stayed_dormant if r.prev_status == TAG_DORMANT else moved_to_dormant).append(r)

run_date_str = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
subject = f"Project Tags Log - {run_date_str}"

body = f"""
<h2>Project Tags Log</h2>
<p><b>Run Time:</b> {format_dt(now)}</p>

{section_html("Moved to Active Projects", moved_to_active)}
{section_html("Stayed on Active Projects", stayed_active)}

{section_html("Moved to Inactive Projects", moved_to_inactive)}
{section_html("Stayed on Inactive Projects", stayed_inactive)}

{section_html("Moved to Dormant Projects", moved_to_dormant)}
{section_html("Stayed on Dormant Projects", stayed_dormant)}
"""

try:
    notify_group(subject, body)
    print(f"Summary email sent: {subject}")
except Exception as e:
    print(f"ERROR sending summary email: {e}")
    traceback.print_exc()
