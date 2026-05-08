# ============================================================
# CELL 2 — Project Tagging
# Evaluates all web maps, updates status tags, builds results
# Output: `tagging_results` list of MapResult
# Depends on: Cell 1 (gis, helpers, constants)
# ============================================================

org_id = gis.properties.id
query = build_webmap_search_query(org_id)
webmaps = search_all_items(gis, query=query)
print(f"Web maps found: {len(webmaps)}")

tagging_results: List[MapResult] = []

for item in webmaps:
    if item.type != "Web Map":
        continue

    tags = list(item.tags) if item.tags else []

    if TAG_COMPLETED in tags:
        continue

    prev_status = status_from_tags(tags)

    last_activity_utc, has_qualifying_layer = get_last_field_activity_for_webmap(item)

    if not has_qualifying_layer:
        continue

    new_status = compute_new_status(last_activity_utc, now)

    # Skip the item.update() API call when neither status has ever been set
    if new_status is None and prev_status is None:
        continue

    set_status_tags(item, new_status)

    if new_status in STATUS_TAGS or prev_status in STATUS_TAGS:
        tagging_results.append(MapResult(
            item_id=item.id,
            title=item.title,
            prev_status=prev_status,
            new_status=new_status,
            last_field_activity_utc=last_activity_utc,
        ))

print(f"Project tagging complete. Managed maps logged: {len(tagging_results)}")
