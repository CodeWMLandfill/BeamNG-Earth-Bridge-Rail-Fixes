"""Railway and tram tracks on the road mesh.

Overture's rail segments (subtype "rail", in the roads layer; each OSM way one track) on the ground (bridge and tunnel
ranges left out) as draped chunks of the road tile: on open ground a ballast bed, the sleepers across it and two rails;
where the track runs on a carriageway (a street tram, a level crossing) West Coast USA's street track instead: its
road_tramline decal, both rails in one 1.8 m ribbon (textureLength 30, as in its DecalRoads). Standard gauge. Flat: no
collision, as the markings. Subways are left out.
The classes railways.py builds as a colliding 3D track on the ground and on bridges (standard and narrow gauge, light
rail: mpsteppe's PR #287) get only their street track here, where they cross a carriageway (railways.py stops
its track at the road's edge). Trams get all their ground track here; mapped tram bridges use railways.py's embedded slab.
"""
import math

import numpy as np
import dnp

import railxing

KINDS = {"standard_gauge": "rail", "narrow_gauge": "narrow", "light_rail": "tram", "tram": "tram"}   # (no "unknown":
# OpenStreetMap's proposed and unbuilt lines, railways.DRAWN)
GAUGE_M = {"rail": 1.435, "narrow": 1.0, "tram": 1.435}
HEAD_M = 0.075                  # a rail head's width; the rail lines lie half of it outside the gauge
TRACK = {                       # ballast top width, sleeper length, sleeper width, sleeper spacing (m)
    "rail": (3.4, 2.6, 0.25, 0.6),
    "narrow": (2.4, 1.7, 0.2, 0.6),
    "tram": (2.6, 2.4, 0.22, 0.6),
}
# stock materials the mod loads (roads.lua MATERIAL_FILES): West Coast USA's road decals, Gridmap v2's grid
BALLAST = {"m": "road_gravel", "tl": 8.0, "p": 19, "layer": "base"}
SLEEPER = {"m": "m_gm_concrete_plain", "tl": 2.0, "p": 4, "layer": "gutter"}
RAIL = {"m": "m_gm_metal_hotspotting", "tl": 4.0, "p": 1, "layer": "mark"}
# a level crossing's deck (BNE_RAIL_XING, railxing.py): a rubber panel strip along each track across the carriageway, its
# two rails on it (from ingest_rail_textures.py's rubber tile)
DECK = {"m": "bne_rail_xing_rubber", "tl": 3.2, "p": 2, "layer": "gutter"}
DECK_EXT = 3                    # samples (m) the deck's rails run on past the carriageway's edge, each side
DECK_RAIL_W = 0.13              # m: a rail's ribbon on the deck
DECK_W = 3.2                    # m: the rails' gauge and 0.9 m of panel outside each
STREET_TRACK = {"m": "road_tramline", "tl": 30.0, "p": 1, "layer": "mark", "w": 1.8}
ON_ROAD_INSET_M = 0.3           # a track point this far inside a carriageway's edge is on the street
SAMPLE_M = 1.0                  # m between the points tested for that
MIN_RUN_M = 2.0                 # shorter open / street runs join their neighbours
MARGIN_M = 4.0                  # ribbons run this far past the tile box (the road mesh cuts them at it)
# Sweden's town trams. A reserved track keeps its middle at least 3.5 m from the road's edge (VGU 2024 K235543, at 40 km/h),
# so one nearer lies in the street, VGU's gatusp?r, not on a bed of its own. OpenStreetMap's embedded tag isn't in the data.
GATUSPAR_M = 3.5
# (3.4 m wide: G?teborg's double tracks lie 3.24-4.19 m apart, half of them, so a pair's paving mostly meets; no official width)
EMBEDDED = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 19, "layer": "base", "w": 3.4}


def _chunk(look, w, pts):
    return {"m": look["m"], "w": round(float(w), 3), "tl": look["tl"], "p": look["p"], "layer": look["layer"],
            "n": [[round(float(p[0]), 3), round(float(p[1]), 3), 0.0] for p in pts]}


def _enu(frame, ll):
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    return np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)


def _lonlat_box(frame, box, m):
    """(lon0, lat0, lon1, lat1) round the frame box (x0, y0, x1, y1) grown by m, and 100 m more (the frame's axes turn
    against the meridians away from its origin): every point within m of the box lies inside it."""
    x0, y0, x1, y1 = box[0] - m - 100.0, box[1] - m - 100.0, box[2] + m + 100.0, box[3] + m + 100.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    return float(np.min(lo)), float(np.min(la)), float(np.max(lo)), float(np.max(la))


def _near(xy, box, m):
    x0, y0, x1, y1 = box
    return xy[:, 0].max() >= x0 - m and xy[:, 0].min() <= x1 + m and xy[:, 1].max() >= y0 - m and xy[:, 1].min() <= y1 + m


def _runs(on):
    """[(start, end, on)] index ranges (end inclusive) of equal values, runs under MIN_RUN_M / SAMPLE_M points
    merged into the one before; each run starts where the one before ends (no gap in the rails). Pure."""
    out = []
    for k, v in enumerate(on):
        if out and out[-1][2] == bool(v):
            out[-1][1] = k
        else:
            out.append([k, k, bool(v)])
    short = int(math.ceil(MIN_RUN_M / SAMPLE_M))
    merged = []
    for r in out:
        if merged and (r[1] - r[0] < short or merged[-1][1] - merged[-1][0] < short):
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    for k in range(1, len(merged)):
        merged[k][0] = merged[k - 1][1]
    return [tuple(r) for r in merged]


def _sleepers(xy, s, step, length, box, on_road):
    """Sleeper ribbons [(a, b)] across the track xy (arcs s) every step m of its arc from its start, the centre within
    the box grown by half a sleeper (each tile's mesh cuts at its edge: the halves meet), none on a carriageway."""
    x0, y0, x1, y1 = box
    g = length / 2 + 0.1
    at = np.arange(step / 2, s[-1], step)
    if not len(at):
        return []
    k = np.clip(np.searchsorted(s, at, side="right") - 1, 0, len(xy) - 2)
    d = xy[k + 1] - xy[k]
    L = np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)
    t = d / L[:, None]
    c = xy[k] + t * (at - s[k])[:, None]
    inside = (c[:, 0] >= x0 - g) & (c[:, 0] <= x1 + g) & (c[:, 1] >= y0 - g) & (c[:, 1] <= y1 + g)
    c, t = c[inside], t[inside]
    if not len(c):
        return []
    keep = ~on_road(c)
    n = np.c_[-t[keep, 1], t[keep, 0]] * (length / 2)
    return list(zip(c[keep] - n, c[keep] + n))


CROSS_WINDOW_M = 30.0           # m of track either side of where it crosses a carriageway's centre line (_crossing_windows)


def _crossing_windows(xy, lines, extra=()):
    """The stretches of track xy (k, 2) within CROSS_WINDOW_M of where it crosses a carriageway centre line (lines), and
    the arc ranges extra [(a, b)] (railways.street_spans: where it runs down a street), overlapping ones joined:
    [(k, 2)]. Pure."""
    import roads
    cr = roads._crossings([xy], lines) if lines else []
    if not cr and not extra:
        return []
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    spans = []
    ranges = [(max(0.0, arc - CROSS_WINDOW_M), min(float(s[-1]), arc + CROSS_WINDOW_M)) for arc in (c[1] for c in cr)]
    for a, b in sorted(ranges + [(float(a), float(b)) for a, b in extra]):
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out = []
    for a, b in spans:
        inner = xy[(s > a) & (s < b)]
        out.append(np.vstack([[dnp.interp(a, s, xy[:, 0]), dnp.interp(a, s, xy[:, 1])], inner,
                              [dnp.interp(b, s, xy[:, 0]), dnp.interp(b, s, xy[:, 1])]]))
    return out


def tram_lines(frame, tx, ty, cells):
    """[(centreline (k, 2), half the track's width)] of the trams on the ground near road tile (tx, ty): what a raised
    sidewalk or median stops at (Sweden's: roads.build_tile)."""
    import roads
    box = roads.tile_bbox(tx, ty)
    out, seen = [], set()
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        hit = (np.asarray(feats.attrs["subtype"]).astype(str) == "rail") & (np.asarray(feats.attrs["class"]).astype(str) == "tram")
        for i in np.flatnonzero(hit):
            fid = str(feats.ids[i])
            for part in roads.ground_parts(feats, i):
                xy = _enu(frame, np.asarray(part, np.float64))
                if len(xy) >= 2 and _near(xy, box, 60.0):
                    pkey = (fid, round(float(xy[0, 0]), 1), round(float(xy[0, 1]), 1),
                            round(float(xy[-1, 0]), 1), round(float(xy[-1, 1]), 1))
                    if pkey in seen:
                        continue
                    seen.add(pkey)
                    out.append((xy, EMBEDDED["w"] / 2))
    return out


def _clip_outside_ways(pts, ways, pad=0.0):
    import roads
    if len(pts) < 2 or not ways:
        return [pts] if len(pts) >= 2 else []
    mw = roads._on_carriageways(pts, ways, pad)
    mid = roads._on_carriageways(0.5 * (pts[:-1] + pts[1:]), ways, pad)
    if not mw.any() and not mid.any():
        return [pts]
    edge_ok = (~mw[:-1]) & (~mw[1:]) & (~mid)
    out, n, i = [], len(edge_ok), 0
    while i < n:
        if not edge_ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and edge_ok[j + 1]:
            j += 1
        out.append(pts[i:j + 2])
        i = j + 1
    return out


def tile_chunks(frame, tx, ty, cells, country=None, urban_fn=None, records=None):
    """The track chunks of road tile (tx, ty) (anchor frame): ballast, sleepers, then the rails (material order: the
    road mesh draws them in that order). cells: the roads Features of the cells touching the tile. Sweden (country),
    in town (urban_fn, as road_specs): a tram beside a carriageway is street track on paving (GATUSPAR_M).
    records: railways.crossing_records of the tile (BNE_RAIL_ROAD_FIT): the level crossings of the 3D tracks then come
    from them alone (crossing_decks: the deck from the track's last point before the crossing to its first after it,
    the cut railways.tile_mesh makes from the same records), none from the carriageways here."""
    import railways
    import roads
    box = roads.tile_bbox(tx, ty)
    reach = max(t[0] for t in TRACK.values()) + MARGIN_M
    carriageways, motorways, town_ways, tracks, seen = {}, {}, {}, [], set()
    road_classes = {}
    sweden = str(country or "").upper() == "SE"
    llb = _lonlat_box(frame, box, reach + 20.0)
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs["class"]
        fb = roads._feature_boxes(feats)        # (lon0, lat0, lon1, lat1) per feature
        far = (fb[:, 2] < llb[0]) | (fb[:, 0] > llb[2]) | (fb[:, 3] < llb[1]) | (fb[:, 1] > llb[3])
        specs = None
        for i in range(len(feats)):
            if far[i]:
                continue                        # (no part of it near the tile: converting each to find that out was
                                                # most of this, 2026-10-25)
            fid = str(feats.ids[i])
            # the lines railways.py builds as a 3D track (mpsteppe's PR #287; railways.drawn, worn ones too): only where
            # they cross a carriageway (its street track across the road, which railways.py leaves to the road),
            # nothing on open ground
            got = railways.drawn(feats, i) if str(sub[i]) == "rail" else None
            meshed = got is not None
            wear = int(got[1]) if meshed else 0
            if str(sub[i]) == "rail" and not meshed and str(cls[i]) != "tram":
                continue
            kind = KINDS.get(got[0] if meshed else str(cls[i])) if str(sub[i]) == "rail" else None
            if str(sub[i]) == "road":
                if specs is None:
                    specs = roads.road_specs(feats, urban_fn if sweden else None)
                if specs[i] is None:
                    continue
            elif kind is None:
                continue
            kept = bool(meshed and wear and railways.RAIL_LIFECYCLE and railways.heritage(feats, i))
            for part in roads.ground_parts(feats, i):
                xy = _enu(frame, np.asarray(part, np.float64))
                if len(xy) < 2 or not _near(xy, box, reach + 20.0):
                    continue
                pkey = ("r" if kind is None else "t", fid,
                        round(float(xy[0, 0]), 1), round(float(xy[0, 1]), 1),
                        round(float(xy[-1, 0]), 1), round(float(xy[-1, 1]), 1))
                if pkey in seen:
                    continue
                seen.add(pkey)
                if kind is None:
                    cname = str(getattr(specs[i], "cls", None) or cls[i])
                    if cname in ("motorway", "motorway_link"):
                        motorways[pkey] = (xy, specs[i].width / 2)
                        continue                # an interstate has no at-grade railway crossing (no street track on it)
                    carriageways[pkey] = (xy, specs[i].width / 2)
                    road_classes[pkey] = cname.removesuffix("_link")
                    if sweden and specs[i].urban:
                        town_ways[pkey] = carriageways[pkey]
                else:
                    tracks.append((kind, xy, meshed, wear, kept))
    fit = railways.RAIL_ROAD_FIT and records is not None
    if not tracks and not fit:
        return []

    def _longitudinal_on_road(run, ways):
        arc = float(np.sum(dnp.hypot(*np.diff(run, axis=0).T)))
        if arc <= 30.0 or not ways:
            return False
        lines, hws = [], []
        rx0, ry0 = float(run[:, 0].min()), float(run[:, 1].min())
        rx1, ry1 = float(run[:, 0].max()), float(run[:, 1].max())
        for ln, hw in ways.values():
            if len(ln) < 2:
                continue
            pad = float(hw) + 2.0
            if (float(ln[:, 0].max()) + pad < rx0 or float(ln[:, 0].min()) - pad > rx1
                    or float(ln[:, 1].max()) + pad < ry0 or float(ln[:, 1].min()) - pad > ry1):
                continue
            lines.append(ln)
            hws.append(float(hw))
        if not lines:
            return False
        for ln, hw, hits in zip(lines, hws, roads._crossings_by([run], lines)):
            for _pi, _arc, pt in hits:
                s_vec = railways._unit_at(run, np.asarray(pt, np.float64))
                r_vec = railways._unit_at(ln, np.asarray(pt, np.float64))
                sin = abs(float(s_vec[0] * r_vec[1] - s_vec[1] * r_vec[0]))
                if sin >= 0.35 and arc <= 2.5 * hw / sin + 6.0:
                    return False
        return True

    def on_road(pts, eligible):
        return roads._on_carriageways(pts, eligible, -ON_ROAD_INSET_M)

    def in_street(pts):
        return roads._on_carriageways(pts, town_ways, GATUSPAR_M) if town_ways else np.zeros(len(pts), bool)

    def on_motorway(pts, pad=0.0):
        return roads._on_carriageways(pts, motorways, pad) if motorways else np.zeros(len(pts), bool)

    def _outside_motorway(pts, pad=0.0):
        return _clip_outside_ways(pts, motorways, pad=pad)

    def no_sleeper(pts, eligible, pad=0.0):
        return on_road(pts, eligible) | in_street(pts) | on_motorway(pts, pad)

    ballast, sleepers, rails = [], [], []
    todo = []
    for kind, xy, meshed, wear, kept in tracks:
        if fit and meshed and kind in ("rail", "narrow"):
            continue                    # (their crossings: the records', below)
        excluded = (railways.HERITAGE_CUT_CLASSES if kept else railways.WORN_CUT_CLASSES).get(wear, ())
        eligible = {key: value for key, value in carriageways.items() if road_classes[key] not in excluded}
        cw_lines = [ln for ln, _hw in eligible.values()]
        if meshed and kind != "tram":
            extra = railways.street_spans(xy, list(eligible.values())) if (wear == 0 or kept) else ()
            todo += [(kind, w, meshed, wear, kept, eligible)
                     for w in _crossing_windows(xy, cw_lines, extra)]
        else:
            todo.append((kind, xy, meshed, wear, kept, eligible))
    for kind, xy, meshed, wear, kept, eligible in todo:
        bw, sl, sw, step = TRACK[kind]
        off = GAUGE_M[kind] / 2 + HEAD_M / 2
        full = roads.densify(xy, SAMPLE_M)
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(full, axis=0).T))]
        sl_pad = sl / 2 + 0.05
        gap = ((lambda q, e=eligible, p=sl_pad: no_sleeper(q, e, p)) if kind == "tram"
               else (lambda q, e=eligible, p=sl_pad: on_road(q, e) | on_motorway(q, p)))
        for a, b in (() if meshed else _sleepers(full, s, step, sl, box, gap)):
            sleepers.append(_chunk(SLEEPER, sw, (a, b)))
        for piece in roads.clip_polyline(full, box[0] - MARGIN_M, box[1] - MARGIN_M, box[2] + MARGIN_M,
                                         box[3] + MARGIN_M):
            piece = roads.densify(piece, SAMPLE_M)
            if len(piece) < 2:
                continue
            for seg in _outside_motorway(piece):
                for a, b, street in _runs(on_road(seg, eligible)):
                    run = seg[a:b + 1]
                    if len(run) < 2:
                        continue
                    if street:
                        if wear >= 1 and not kept and _longitudinal_on_road(run, eligible):
                            continue
                        if railxing.ENABLED and meshed and wear < 2 and kind in ("rail", "narrow"):
                            # (past the carriageway's edge: the 3D track stops a sample (2 m) or so short of it, the deck's
                            # panels reach a metre beyond the road and its rails (wider than the open track's: they read
                            # as steel on the dark rubber) run on until they meet it)
                            lo, hi = max(0, a - DECK_EXT), min(len(seg) - 1, b + DECK_EXT)
                            for dpts in _outside_motorway(seg[max(0, a - 1):min(len(seg) - 1, b + 1) + 1]):
                                sleepers.append(_chunk(DECK, DECK_W, dpts))
                            for side in (off, -off):
                                for rpts in _outside_motorway(roads.offset_polyline(seg[lo:hi + 1], side)):
                                    rails.append(_chunk(RAIL, DECK_RAIL_W, rpts))
                            continue
                        rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], run))
                        continue
                    if meshed:
                        continue
                    for c, d, paved in (_runs(in_street(run)) if kind == "tram" else [(0, len(run) - 1, False)]):
                        part = run[c:d + 1]
                        if len(part) < 2:
                            continue
                        if paved:
                            for p_in in _outside_motorway(part, EMBEDDED["w"] / 2):
                                ballast.append(_chunk(EMBEDDED, EMBEDDED["w"], p_in))
                                rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], p_in))
                            continue
                        for p_in in _outside_motorway(part, bw / 2):
                            ballast.append(_chunk(BALLAST, bw, p_in))
                            for side in (off, -off):
                                for rpts in _outside_motorway(roads.offset_polyline(p_in, side), HEAD_M / 2):
                                    rails.append(_chunk(RAIL, HEAD_M, rpts))
    if fit:
        crossing_decks(records, box, sleepers, rails, motorways=motorways)
    return ballast + sleepers + rails


def crossing_decks(records, box, sleepers, rails, motorways=None):
    """railways.RAIL_ROAD_FIT: the level crossings of railways.crossing_records (railways.crossing_panels: from the 3D
    track's last point before each crossing to its first after it) as chunks of the tile (box): with BNE_RAIL_XING the
    rubber deck (into sleepers) and its two rails at the 3D track's gauge (into rails), else the street track decal;
    each cut MARGIN_M past the tile box (the road mesh cuts them at it, the next tile lays the rest from its own records:
    the same crossing)."""
    import roads
    for _fid, _cls, line, half_g in sorted(_panels(records), key=lambda e: (e[0], float(e[2][0, 0]), float(e[2][0, 1]))):
        for piece in roads.clip_polyline(line, box[0] - MARGIN_M, box[1] - MARGIN_M, box[2] + MARGIN_M,
                                         box[3] + MARGIN_M):
            piece = roads.densify(piece, SAMPLE_M)[:, :2]
            for seg in _clip_outside_ways(piece, motorways):
                if railxing.ENABLED:
                    sleepers.append(_chunk(DECK, DECK_W, seg))
                    for side in (half_g, -half_g):
                        for rpts in _clip_outside_ways(roads.offset_polyline(seg, side), motorways):
                            rails.append(_chunk(RAIL, DECK_RAIL_W, rpts))
                else:
                    rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], seg))


def _panels(records):
    import railways
    return railways.crossing_panels(records)
