# ============================================================
# CELL 3 — Data Health Reports
# Generates JPGs, posts to Google Sheets
# Output: `confidence_scores` dict {item_id: float pct}
# Depends on: Cell 1 (gis, helpers, constants, all functions)
# ============================================================

run_dt = datetime.now()
as_of_date = run_dt.date()
generated_text = run_dt.strftime("%Y-%m-%d %I:%M %p")

confidence_scores = {}

dh_query = f'tags:"{TAG_ACTIVE}" AND type:"Web Map"'
dh_webmaps = gis.content.search(query=dh_query, max_items=400)

print(
    f"Found {len(dh_webmaps)} web map(s) "
    f"tagged '{TAG_ACTIVE}' for data health processing"
)

for wm in dh_webmaps:

    try:

        print("\n----------------------------")
        print("Web Map:", wm.title)

        data = wm.get_data() or {}
        base_title = wm.title.strip()
        image_title = f"{base_title} Data Health Report"

        irrigation_items = []
        drainage_items = []
        golf_items = []
        score_sections = []
        score_irrigation = []
        score_drainage = []
        score_golf = []
        collector_scores = {}

        # -------------------------
        # HEADS
        # -------------------------

        head_total = 0
        head_filled = 0

        for layer_title, head_field in HEAD_LAYER_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)

            if not fl:
                continue

            total = count_where(fl, "1=1")
            filled = count_where(fl, f"NOT {is_null_or_empty_sql(head_field)}")

            head_total += total
            head_filled += filled

            collector_field_scores(
                fl,
                collector_scores,
                "Heads",
                value_field=head_field,
                weight=WEIGHTS["Heads"]
            )

        if head_total > 0:
            irrigation_items.append(
                build_count_item("Head type collected", head_total, head_filled)
            )
            score_irrigation.append({
                "label": "Heads",
                "earned": head_filled * WEIGHTS["Heads"],
                "possible": head_total * WEIGHTS["Heads"]
            })

        # -------------------------
        # IRRIGATION POINTS
        # -------------------------

        for group_label, layer_title, field_name in IRRIGATION_POINT_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)
            total_count = count_where(fl, "1=1")

            if total_count <= 0:
                continue

            ok_count = count_where(fl, f"NOT {is_null_or_empty_sql(field_name)}")

            irrigation_items.append(
                build_count_item(f"{group_label} size collected", total_count, ok_count)
            )
            score_irrigation.append({
                "label": group_label,
                "earned": ok_count * WEIGHTS[group_label],
                "possible": total_count * WEIGHTS[group_label]
            })
            collector_field_scores(
                fl,
                collector_scores,
                group_label,
                value_field=field_name,
                weight=WEIGHTS[group_label]
            )

        # -------------------------
        # IRRIGATION PIPE
        # -------------------------

        for group_label, layer_title, field_name in IRRIGATION_PIPE_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)
            total_len = sum_field(fl, PIPE_LENGTH_FIELD, "1=1")

            if total_len <= 0:
                continue

            missing_len = sum_field(fl, PIPE_LENGTH_FIELD, is_null_or_empty_sql(field_name))
            ok_len = max(0.0, total_len - missing_len)

            irrigation_items.append(
                build_length_item(f"{group_label} size collected", total_len, ok_len)
            )
            score_irrigation.append({
                "label": group_label,
                "earned": ok_len * WEIGHTS[group_label],
                "possible": total_len * WEIGHTS[group_label]
            })
            collector_field_scores(
                fl,
                collector_scores,
                group_label,
                value_field=field_name,
                length_field=PIPE_LENGTH_FIELD,
                weight=WEIGHTS[group_label]
            )

        # -------------------------
        # DRAINAGE POINTS
        # -------------------------

        for group_label, layer_title, field_name in DRAINAGE_POINT_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)
            total_count = count_where(fl, "1=1")

            if total_count <= 0:
                continue

            ok_count = count_where(fl, f"NOT {is_null_or_empty_sql(field_name)}")

            drainage_items.append(
                build_count_item(f"{group_label} size collected", total_count, ok_count)
            )
            score_drainage.append({
                "label": group_label,
                "earned": ok_count * WEIGHTS[group_label],
                "possible": total_count * WEIGHTS[group_label]
            })
            collector_field_scores(
                fl,
                collector_scores,
                group_label,
                value_field=field_name,
                weight=WEIGHTS[group_label]
            )

        # -------------------------
        # DRAINAGE PIPE
        # -------------------------

        for group_label, layer_title, field_name in DRAINAGE_PIPE_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)
            total_len = sum_field(fl, PIPE_LENGTH_FIELD, "1=1")

            if total_len <= 0:
                continue

            missing_len = sum_field(fl, PIPE_LENGTH_FIELD, is_null_or_empty_sql(field_name))
            ok_len = max(0.0, total_len - missing_len)

            drainage_items.append(
                build_length_item(f"{group_label} size collected", total_len, ok_len)
            )
            score_drainage.append({
                "label": group_label,
                "earned": ok_len * WEIGHTS[group_label],
                "possible": total_len * WEIGHTS[group_label]
            })
            collector_field_scores(
                fl,
                collector_scores,
                group_label,
                value_field=field_name,
                length_field=PIPE_LENGTH_FIELD,
                weight=WEIGHTS[group_label]
            )

        # -------------------------
        # GOLF LAYERS
        # -------------------------

        for group_label, layer_title in GOLF_LAYER_SPECS:

            url = find_layer_url(data, layer_title)
            fl = safe_fl(url)

            analysis = analyze_golf_layer(
                fl,
                group_label,
                collector_scores=collector_scores,
                weight=WEIGHTS.get(group_label, 1.0)
            )

            if analysis["feature_total"] <= 0:
                continue

            golf_items.append(
                build_golf_item(
                    group_label,
                    analysis["feature_ok"],
                    analysis["feature_total"],
                    analysis["bullet_lines"]
                )
            )
            score_golf.append({
                "label": group_label,
                "earned": analysis["earned_units"],
                "possible": analysis["possible_units"]
            })

        # -------------------------
        # SCORE SECTIONS
        # -------------------------

        if score_irrigation:
            score_sections.append({"title": "Irrigation", "rows": score_irrigation})

        if score_drainage:
            score_sections.append({"title": "Drainage", "rows": score_drainage})

        if score_golf:
            score_sections.append({"title": "Golf Features", "rows": score_golf})

        total_earned_points = sum(
            r["earned"] for sec in score_sections for r in sec["rows"]
        )
        total_possible_points = sum(
            r["possible"] for sec in score_sections for r in sec["rows"]
        )

        if total_possible_points > 0:
            confidence_scores[wm.id] = pct(total_earned_points, total_possible_points)

        collectors = sorted({
            row["collector"]
            for row in collector_scores.values()
            if row["collector"] not in (None, "", "Unknown")
        })

        metrics = {
            "as_of_week": as_of_date,
            "generated_text": generated_text,
            "irrigation_items": irrigation_items,
            "drainage_items": drainage_items,
            "golf_items": golf_items,
            "collectors": collectors,
            "total_earned_points": total_earned_points,
            "total_possible_points": total_possible_points,
            "score_sections": score_sections,
        }

        safe_name = safe_filename(base_title)
        local_jpg = os.path.join(OUT_DIR, f"{safe_name}.jpg")

        render_full_report_image(
            metrics,
            local_jpg,
            title_base=base_title,
            width_px=IMG_WIDTH
        )

        item, action = upsert_jpeg_item(local_jpg, image_title, folder_title=TARGET_FOLDER)
        print(f"  JPG {action}: {item.title} | ItemID: {item.id}")

        try:
            share_method = make_item_public(item)
            print(f"  Public sharing applied via: {share_method}")
        except Exception as e:
            print(f"  WARNING: item created/updated but could not make public: {e}")

        # -------------------------
        # PROJECT-LEVEL SHEETS ROW
        # -------------------------

        def _spct(sec_title, label):
            for sec in score_sections:
                if sec['title'] == sec_title:
                    for row in sec['rows']:
                        if row['label'] == label:
                            p = row['possible']
                            return round(pct(row['earned'], p), 1) if p else 0.0
            return 0.0

        def _stot(sec_title):
            for sec in score_sections:
                if sec['title'] == sec_title:
                    e = sum(r['earned'] for r in sec['rows'])
                    p = sum(r['possible'] for r in sec['rows'])
                    return round(pct(e, p), 1) if p else 0.0
            return 0.0

        def _find(items, frag):
            frag = frag.lower()
            for it in items:
                if frag in it['label'].lower():
                    return it
            return {}

        def _t(items, frag): return round(_find(items, frag).get('total',  0) or 0, 1)
        def _f(items, frag): return round(_find(items, frag).get('filled', 0) or 0, 1)
        def _i(items, frag):
            it = _find(items, frag)
            return round(max(0, (it.get('total', 0) or 0) - (it.get('filled', 0) or 0)), 1)

        payload = {
            "record_key":            f"{wm.id}_{as_of_date.isoformat()}",
            "row_type":              "project_score",
            "run_date":              as_of_date.isoformat(),
            "run_timestamp":         generated_text,
            "project_name":          base_title,
            "project_item_id":       wm.id,
            "confidence_pct":        round(pct(total_earned_points, total_possible_points), 1),
            "total_points_earned":   round(total_earned_points, 1),
            "total_points_possible": round(total_possible_points, 1),
            "collector_count":       len(collectors),
            "collectors":            "|".join(collectors),

            # Section-level pcts
            "irrigation_pct": _stot('Irrigation'),
            "drainage_pct":   _stot('Drainage'),
            "golf_pct":       _stot('Golf Features'),

            # Per-category pcts
            "heads_pct":         _spct('Irrigation', 'Heads'),
            "quick_pct":         _spct('Irrigation', 'Quick Couplers'),
            "iso_pct":           _spct('Irrigation', 'Isolation Valves'),
            "lateral_valve_pct": _spct('Irrigation', 'Lateral Valves'),
            "lateral_pct":       _spct('Irrigation', 'Lateral Pipe'),
            "mainline_pct":      _spct('Irrigation', 'Mainline'),
            "basins_pct":        _spct('Drainage',   'Basins'),
            "perf_pct":          _spct('Drainage',   'Perf Drainage'),
            "solid_pct":         _spct('Drainage',   'Solid Drainage'),
            "greens_pct":        _spct('Golf Features', 'Greens'),
            "tees_pct":          _spct('Golf Features', 'Tees'),
            "bunkers_pct":       _spct('Golf Features', 'Bunkers'),

            # Heads
            "heads_total":      _t(irrigation_items, 'head type'),
            "heads_complete":   _f(irrigation_items, 'head type'),
            "heads_incomplete": _i(irrigation_items, 'head type'),

            # Quick Couplers
            "quick_total":      _t(irrigation_items, 'quick couplers'),
            "quick_complete":   _f(irrigation_items, 'quick couplers'),
            "quick_incomplete": _i(irrigation_items, 'quick couplers'),

            # Isolation Valves
            "iso_total":      _t(irrigation_items, 'isolation valves'),
            "iso_complete":   _f(irrigation_items, 'isolation valves'),
            "iso_incomplete": _i(irrigation_items, 'isolation valves'),

            # Lateral Valves
            "lateral_valve_total":      _t(irrigation_items, 'lateral valves'),
            "lateral_valve_complete":   _f(irrigation_items, 'lateral valves'),
            "lateral_valve_incomplete": _i(irrigation_items, 'lateral valves'),

            # Lateral Pipe (ft)
            "lateral_ft_total":      _t(irrigation_items, 'lateral pipe'),
            "lateral_ft_complete":   _f(irrigation_items, 'lateral pipe'),
            "lateral_ft_incomplete": _i(irrigation_items, 'lateral pipe'),

            # Mainline (ft)
            "mainline_ft_total":      _t(irrigation_items, 'mainline'),
            "mainline_ft_complete":   _f(irrigation_items, 'mainline'),
            "mainline_ft_incomplete": _i(irrigation_items, 'mainline'),

            # Basins
            "basins_total":      _t(drainage_items, 'basins'),
            "basins_complete":   _f(drainage_items, 'basins'),
            "basins_incomplete": _i(drainage_items, 'basins'),

            # Perf Drainage (ft)
            "perf_ft_total":      _t(drainage_items, 'perf drainage'),
            "perf_ft_complete":   _f(drainage_items, 'perf drainage'),
            "perf_ft_incomplete": _i(drainage_items, 'perf drainage'),

            # Solid Drainage (ft)
            "solid_ft_total":      _t(drainage_items, 'solid drainage'),
            "solid_ft_complete":   _f(drainage_items, 'solid drainage'),
            "solid_ft_incomplete": _i(drainage_items, 'solid drainage'),

            # Greens
            "greens_features_total": _t(golf_items, 'greens'),
            "greens_complete":       _f(golf_items, 'greens'),
            "greens_incomplete":     _i(golf_items, 'greens'),

            # Tees
            "tees_features_total": _t(golf_items, 'tees'),
            "tees_complete":       _f(golf_items, 'tees'),
            "tees_incomplete":     _i(golf_items, 'tees'),

            # Bunkers
            "bunkers_features_total": _t(golf_items, 'bunkers'),
            "bunkers_complete":       _f(golf_items, 'bunkers'),
            "bunkers_incomplete":     _i(golf_items, 'bunkers'),
        }

        ok, msg = post_to_google_sheets(payload)
        print(f"  Sheets logging: {'OK' if ok else 'FAILED'} | {msg}")

        post_collector_scores_to_sheets(
            as_of_date=as_of_date,
            generated_text=generated_text,
            base_title=base_title,
            webmap_item_id=wm.id,
            collector_scores=collector_scores
        )

        try:
            os.remove(local_jpg)
        except Exception:
            pass

    except Exception:
        print("ERROR processing:", wm.title)
        traceback.print_exc()

print(
    f"\nData health complete. "
    f"Confidence scores computed: {len(confidence_scores)}"
)
