"""Footbridges (GitHub #905: a footbridge over a river in Gunma, 36.3581 N 138.6982 E, was not there; its path ran on
the ground). The walkways are not drawn as roads (roads.STYLE leaves them out), so their bridges had no deck at all.

A walkway's bridge pieces (the classes grading.WALK_SPAN_CLASSES, OSM bridge=*) get a concrete slab, parapets and
piers, in the road tile's railway mesh (railways.tile_mesh: drawn, and colliding through its lean collider). The deck
runs straight between the ground at its ends (on a bank it may rest on the ground). Only where that line clears what
it crosses: a carriageway under it by CLEAR_ROAD_M, the ground by CLEAR_GROUND_M somewhere, never buried more than
BURIED_M (inside END_M of its ends it may meet anything). A footbridge
over a street at grade in the elevation data (an overpass whose stairs or ramps the map has as other ways) would
need those climbs, and a slab at the street's level would block it: those are left out, as before.
Walkway tunnels stay as they are (the walkway is not drawn on the ground either).
"""
import numpy as np

WALK_CLASSES = ("footway", "path", "pedestrian", "cycleway", "bridleway", "steps")   # (grading.WALK_SPAN_CLASSES)
STEP_M = 2.0                # m between cross-sections (railways.STEP_M)
WIDTH_M = {"cycleway": 3.0, "pedestrian": 3.0, "bridleway": 2.5}    # m, a deck's width by class (OSM width first)
WIDTH_DEFAULT_M = 2.0
WIDTH_RANGE_M = (1.0, 8.0)  # an OSM width outside this is ignored
DECK_D = 0.45               # m of slab under the walking surface
PARAPET_H, PARAPET_T = 1.1, 0.15    # m
END_M = 3.0                 # m from either end where the deck may meet the ground (its abutments)
CLEAR_GROUND_M = 1.0        # m the deck's underside stands over the ground somewhere between those, at least
BURIED_M = 0.5              # m: the ground standing higher over the walking surface than this between them: no deck
CLEAR_ROAD_M = 4.5          # m its underside stands over a carriageway under it, at least
LEN_RANGE_M = (4.0, 2500.0) # a bridge piece's length (shorter: a culvert's boardwalk; longer: bad data)
PIER_EVERY = 20.0           # m between piers
PIER_ALONG = 0.6            # m
PIER_MIN_H = 2.0            # m: a lower deck stands on its ends alone
REACH_M = 30.0              # m round the tile the walkway bridges are taken


def _box_hit(b, box):
    return b[0] <= box[2] and b[2] >= box[0] and b[1] <= box[3] and b[3] >= box[1]


def _width(feats, i, cls):
    import roads
    w = roads._rule_value(feats.rules.get("width"), i)
    if w is None:
        w = roads._osm(feats, "width", i, *roads.OSM_WIDTH_M)
    try:
        w = float(w) if w is not None else None
    except (TypeError, ValueError):
        w = None
    if w is not None and WIDTH_RANGE_M[0] <= w <= WIDTH_RANGE_M[1]:
        return w
    return WIDTH_M.get(cls, WIDTH_DEFAULT_M)


def parts(frame, cells, box, reach=REACH_M):
    """[(fid, xy (k, 2) every STEP_M, width m)]: the walkways' bridge pieces near box (x0, y0, x1, y1), each feature
    once (by id), in id order."""
    import mapgeom
    import railways
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "class" not in feats.attrs:
            continue
        sub = feats.attrs.get("subtype")
        cls = np.asarray(feats.attrs["class"]).astype(str)
        hit = np.isin(cls, WALK_CLASSES)
        if sub is not None:
            hit &= np.asarray(sub).astype(str) == "road"
        fb = None
        for i in np.flatnonzero(hit):
            c = str(cls[i])
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            fb = feats.boxes(frame) if fb is None else fb
            if not _box_hit(fb[i], near):
                continue
            seen.add(fid)
            for kind, rings in railways._pieces(feats, i):
                if kind != "bridge":
                    continue
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) < 2 or not _box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()),
                                                   near):
                        continue
                    xy = railways._resample(xy, STEP_M)
                    L = float(railways._arc(xy)[-1]) if len(xy) >= 2 else 0.0
                    if LEN_RANGE_M[0] <= L <= LEN_RANGE_M[1]:
                        out.append((fid, xy, _width(feats, i, c)))
    out.sort(key=lambda p: (p[0], float(p[1][0, 0]), float(p[1][0, 1])))
    return out


ROAD_PARAPET_W = 0.45   # m past a road bridge's carriageway edge (roadmesh.PARAPET_W)
BRIDGE_UNDER_M = 3.0    # m below a road bridge deck where slab/girders and headroom exclude a footbridge


def _line_dist(xy, line):
    """Per point of xy (k, 2): (min_dist, seg_idx, seg_t) to polyline line (m, 2)."""
    a, b = line[:-1], line[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((xy[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0.0, 1.0)
    q = a[None] + t[..., None] * d[None]
    dist = np.hypot(*(xy[:, None, :] - q).transpose(2, 0, 1))
    j = np.argmin(dist, 1)
    k = np.arange(len(xy))
    return dist[k, j], j, t[k, j]


def _over_roads(xy, crossers):
    """Per point of xy: on a carriageway (crossers: [(centreline, half width, z)])."""
    on = np.zeros(len(xy), bool)
    for line, hw, _z in crossers:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        if (xy[:, 0].max() < line[:, 0].min() - hw or xy[:, 0].min() > line[:, 0].max() + hw or
                xy[:, 1].max() < line[:, 1].min() - hw or xy[:, 1].min() > line[:, 1].max() + hw):
            continue
        on |= _line_dist(xy, line)[0] <= hw
    return on


def _over_road_bridges(xy, z, half, road_bridges):
    """Per point of xy: laterally overlapping a road bridge deck+parapet without vertical separation."""
    hit = np.zeros(len(xy), bool)
    for line, hw, zb in road_bridges:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = hw + ROAD_PARAPET_W + half + PARAPET_T
        if (xy[:, 0].max() < line[:, 0].min() - reach or xy[:, 0].min() > line[:, 0].max() + reach or
                xy[:, 1].max() < line[:, 1].min() - reach or xy[:, 1].min() > line[:, 1].max() + reach):
            continue
        dist, j, tj = _line_dist(xy, line)
        near = dist <= reach
        if not near.any():
            continue
        if zb is None:
            hit |= near
            continue
        zb = np.asarray(zb, np.float64)
        if zb.ndim == 0 or len(zb) < len(line):
            zr = np.full(len(xy), float(zb.reshape(-1)[0]))
        else:
            zr = zb[j] + tj * (zb[j + 1] - zb[j])
        hit |= near & np.isfinite(zr) & (z - DECK_D - zr < CLEAR_ROAD_M) & (z >= zr - BRIDGE_UNDER_M)
    return hit


def _with_heights(road_bridges, ground):
    """Attach deck height arrays to [(line, hw)] or [(line, hw, z)] road bridge entries."""
    items = list(road_bridges)
    if not items:
        return []
    need = [i for i, item in enumerate(items) if len(item) < 3 or item[2] is None]
    if need:
        import roads
        zbs = roads.deck_heights([np.asarray(items[i][0], np.float64)[:, :2] for i in need], ground)
        for i, zb in zip(need, zbs):
            items[i] = (items[i][0], items[i][1], zb)
    return [
        (np.asarray(it[0], np.float64)[:, :2], float(it[1]), np.asarray(it[2], np.float64))
        for it in items if len(it) >= 3 and it[2] is not None and len(it[0]) >= 2
    ]


def deck(xy, zg, crossers, road_bridges=(), half=0.0):
    """The walking surface's heights (k,) of a bridge piece xy (k, 2) over the ground zg (k,) there (the drawn ground:
    a road under it is its surface), or None where the straight line between its ends does not clear what it crosses
    (CLEAR_*) or collides with road_bridges ([(centreline, half width, z)]). Pure."""
    import railways
    s = railways._arc(xy)
    L = float(s[-1])
    if L <= 0.0 or not np.isfinite(zg).all():
        return None
    z = zg[0] + (zg[-1] - zg[0]) * s / L
    mid = (s >= END_M) & (s <= L - END_M)
    if not mid.any():
        return None
    room = z - DECK_D - zg
    road = _over_roads(xy, crossers)
    if (room[mid & road] < CLEAR_ROAD_M).any():
        return None                 # (a slab across a carriageway at its level)
    if road_bridges and (_over_road_bridges(xy, z, half, road_bridges) & mid).any():
        return None                 # (parallel walkway on or colliding with a road bridge deck)
    if (z[mid] < zg[mid] - BURIED_M).any() or room[mid].max() < CLEAR_GROUND_M:
        return None                 # (in the ground: the elevation's own bridge, a hill; or nothing under it)
    return z


def add(mesh, bridges, ground, crossers, ex, concrete, small, road_bridges=()):
    """The decks of bridges (parts') into railways' _Mesh mesh: the slab and its parapets over the points within ex (the
    tile's box with its overlap), the piers into small (railways.tile_mesh draws them with the sleepers, as room
    allows). ground(xy (n, 2)) -> z (one call for all of them); road_bridges: [(line, half)] or [(line, half, z)]."""
    import railways
    if not bridges:
        return
    rbridges = _with_heights(road_bridges, ground)
    pier_cross = list(crossers) + rbridges if rbridges else crossers
    allxy = np.concatenate([xy for _, xy, _ in bridges])
    zg_all = np.asarray(ground(allxy), np.float64)
    up = np.array([0.0, 0.0, 1.0])
    k = 0
    for _fid, xy, w in bridges:
        zg = zg_all[k:k + len(xy)]
        k += len(xy)
        half = float(w) / 2.0
        z = deck(xy, zg, crossers, rbridges, half=half)
        if z is None:
            continue
        inside = (xy[:, 0] >= ex[0] - STEP_M) & (xy[:, 0] <= ex[2] + STEP_M) & \
                 (xy[:, 1] >= ex[1] - STEP_M) & (xy[:, 1] <= ex[3] + STEP_M)
        if not inside.any():
            continue
        u = railways._left(xy)
        s = railways._arc(xy)
        for i, j in railways._runs(~inside):
            sl = slice(i, j + 1)
            p, uu, t, v = xy[sl], u[sl], z[sl], s[sl] / 3.0
            out_l = np.c_[uu, np.zeros(len(p))]
            left, right = p + uu * half, p - uu * half
            mesh.strip(np.c_[left, t], np.c_[right, t], 0.0, w / 3.0, v, concrete, up)
            mesh.strip(np.c_[left, t - DECK_D], np.c_[right, t - DECK_D], 0.0, w / 3.0, v, concrete, -up)
            for sg in (1, -1):
                o = out_l * sg
                e_out, e_in = p + uu * sg * (half + PARAPET_T), p + uu * sg * half
                hi = t + PARAPET_H
                mesh.strip(np.c_[e_in, t], np.c_[e_in, hi], 0.0, 0.3, v, concrete, -o)
                mesh.strip(np.c_[e_in, hi], np.c_[e_out, hi], 0.0, 0.05, v, concrete, up)
                mesh.strip(np.c_[e_out, hi], np.c_[e_out, t - DECK_D], 0.0, 0.5, v, concrete, o)
            pr = _piers(s, xy, u, z, zg, i, j)
            pr = railways._sited(pr, pier_cross, ground) if pr is not None else None
            if pr is not None:
                small.append((pr, PIER_ALONG / 2, max(half - 0.2, 0.4), concrete))


def _piers(s, xy, u, z, zg, i, j):
    """(centres, along, across, z0, z1) of the piers under run i..j, every PIER_EVERY from the piece's start, where the
    slab's underside stands PIER_MIN_H or more over the ground (railways._piers' shape)."""
    ss = np.arange(PIER_EVERY, s[-1] - PIER_EVERY / 2, PIER_EVERY)
    ss = ss[(ss >= s[i]) & (ss <= s[j])]
    if not len(ss):
        return None
    zt = np.interp(ss, s, z) - DECK_D
    zgs = np.interp(ss, s, zg)
    keep = zt - zgs >= PIER_MIN_H
    ss, zt, zgs = ss[keep], zt[keep], zgs[keep]
    if not len(ss):
        return None
    import railways
    cx, cy = np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])
    ux, uy = np.interp(ss, s, u[:, 0]), np.interp(ss, s, u[:, 1])
    un = np.c_[ux, uy] / np.maximum(np.hypot(ux, uy), 1e-9)[:, None]
    return np.c_[cx, cy], np.c_[un[:, 1], -un[:, 0]], un, zgs - railways.PIER_FOOT_M, zt
