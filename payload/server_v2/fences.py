"""Mapped chain-link fences and noise walls (Overture infrastructure lines).

OpenStreetMap's barrier=fence with fence_type=chain_link, and barrier=wall with wall=noise_barrier (or
barrier=noise_barrier), kept on the feature's class and in source_tags; stone walls (wall=dry_stone ...) as
stone meshes (stone_walls). In France with PR #505's European road details on (streetfront.country_at_tile), the
mapped fences, walls and hedges take regional panels and the stock shrub instead (french_rows), and yards get inferred
boundaries (inferred_rows). Other fences and walls are left out. Straight stock panels along the line: West Coast USA's chain-link panel (s_chainlink_old, 2.04 m,
origin at its forward end) and its concrete noise blocker (s_concrete_noiseblocker, 4 m along local +y,
origin at one end). A panel is kept when its centre is in the tile, so a line split across tiles is not
drawn twice.
"""
from collections import OrderedDict
import json
import logging
import math
import os
import threading

import numpy as np
import dmath
import dnp

import mapcells
import mapgeom
from guardrails import DECK_AUTO_SNAP_TOLERANCE_M, DECK_WIDE_REACH_M, _deck_level_hint, _surface_info

log = logging.getLogger("earthd.fences")

_NOISE_LOCK = threading.Lock()
_MESHED_NOISE = OrderedDict()
_PENDING_BRIDGE_NOISE = OrderedDict()
_NOISE_CACHE_MAX = 256


def _box_key(box):
    return tuple(round(float(v), 2) for v in box)


def _origin_key(xy):
    return (round(float(xy[0]), 2), round(float(xy[1]), 2))


def _record_meshed_noise(frame, box, fids, replace=False):
    key = (id(frame), _box_key(box))
    with _NOISE_LOCK:
        if replace or key not in _MESHED_NOISE:
            _MESHED_NOISE[key] = set(fids)
        else:
            _MESHED_NOISE[key].update(fids)
        _MESHED_NOISE.move_to_end(key)
        while len(_MESHED_NOISE) > _NOISE_CACHE_MAX:
            _MESHED_NOISE.popitem(last=False)


def _get_meshed_noise(frame, box):
    key = (id(frame), _box_key(box))
    with _NOISE_LOCK:
        got = _MESHED_NOISE.get(key)
        return set(got) if got else set()


def _stash_pending_bridge_noise(box, payload):
    key = _origin_key(box[:2])
    with _NOISE_LOCK:
        _PENDING_BRIDGE_NOISE[key] = payload
        _PENDING_BRIDGE_NOISE.move_to_end(key)
        while len(_PENDING_BRIDGE_NOISE) > _NOISE_CACHE_MAX:
            _PENDING_BRIDGE_NOISE.popitem(last=False)


def take_bridge_noise_walls(origin, chunks):
    """Resolve bridge-tagged noise walls deferred from stone_walls() against tile deck chunks."""
    if not NOISE_ON or not chunks:
        return []
    key = _origin_key(origin)
    with _NOISE_LOCK:
        payload = _PENDING_BRIDGE_NOISE.pop(key, None)
    if payload is None:
        return []
    frame, infra_fn, box, elev, roads = payload
    road_decks = [{"n": c["n"], "w": c["w"]} for c in chunks if c.get("deck") and not c.get("tunnel")]
    if not road_decks:
        return []
    meshed = set()
    nw = noise_walls(frame, infra_fn, box, elev, decks=road_decks, roads=roads,
                     only_bridge=True, meshed_out=meshed)
    if meshed:
        _record_meshed_noise(frame, box, meshed, replace=False)
    return nw

G_CHAIN, G_WALL = "chainlink", "noisewall"
G_HEDGE = "fr_hedge"           # (PR #505: French hedges, the stock shrub)
# s_chainlink_old: x -2.02..0.02, z -0.16..1.60. The centre is 1 m behind the origin along local +x.
CHAIN_LEN, CHAIN_OX, CHAIN_H, CHAIN_LIFT = 2.04, 1.0, 1.76, 0.16
# s_concrete_noiseblocker: x 0..2.5 (thickness), y 0..4 (along the wall), z 0..2.4. Centre (1.25, 2).
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
# its flat face is the full-height one at local x = 2.5; the other side has a 0.48 m base and a slope up
# (the mesh: 9.6 m2 of face at +x against 2.0 m2 at -x), so a panel is laid with local +x toward the road.
# A noise wall stands off the carriageway it runs along, not on the mapped line the way happens to have: the line
# follows the real deck's outer edge, which is a shoulder's width outside the carriageway drawn here. It is laid
# along the carriageway, WALL_EDGE_M beyond its painted edge, with the flat face toward it.
WALL_EDGE_M = 0.5           # m between the carriageway's edge and the wall's face (its inner side)
WALL_FIT_REACH_M = 20.0     # m: a wall farther from every carriageway keeps its mapped line
WALL_FIT_MAX_DEG = 40.0     # a wall this far off the carriageway's direction is not laid along it
WALL_ACROSS_DEG = 50.0      # ... and over the carriageway: never drawn, the wall must not cross the road
WALL_ON_ROAD_M = 0.3        # m past a carriageway's edge no wall panel stands, whatever its direction (GitHub #1022)
WALL_LEVEL_M = 3.0          # m: a carriageway this far above or below the wall is not the one it runs along
WALL_DECK_REACH_M = 4.0     # m past a deck's edge a bridge wall (its own layer/bridge tags) still stands on it
                            # (a road passing under a rail viaduct)
NOISE_ON = os.environ.get("BNE_NOISE_WALLS", "on") != "off"
NOISE_THICK_M = 0.5         # (roadmesh.WALL_COPING_M: all of its top is concrete, no soil strip)
NOISE_FOOT_M = 0.3
NOISE_STEP_M = 1.0          # continuous vertex spacing along noise barriers (grade-following mesh)
NOISE_JOG_MAX_M = 4.0       # m: short acoustic baffle zig-zag excursions collapsed before resampling
NOISE_JOG_COS_MIN = 0.7     # cos(~45 deg): alignment required across a collapsed baffle jog
NOISE_GAP_SNAP_M = 8.0      # m: short non-parallel gap between carriageway-fitted runs snapped inline
CHAIN_TYPES = {"chain_link", "chainlink", "chain-link", "chain"}
NOISE_TYPES = {"noise_barrier", "noisebarrier", "sound_barrier"}
BBOX_PAD_M = 8.0
ALONG_EDGE_M = 4.0      # m past a deck's edge an untagged barrier running along it is its parapet (ALONG_*)
ALONG_DEG = 15.0        # within this angle of the deck's direction
ALONG_FRAC = 0.7        # ... for this share of its panels
PARAPET_ON = os.environ.get("BNE_FENCE_PARAPET", "on") != "off"
CHAIN_OFF_ROAD_ON = os.environ.get("BNE_FENCE_OFF_ROAD", "on") != "off"
OUTLINE_PAD_M = 1.0     # m past a bridge outline's edge a barrier along it still counts as on it (both mapped by hand)
# PR #505 (exencendre): French boundary panels and hedges
PANEL_M = 2.2
HEDGE_STEP_M = 1.15
# The Forest instance scales the stock shrub uniformly, including its crown.
HEDGE_CROWN_PER_M = 1.0
HEDGE_ROAD_MARGIN_M = .15
HEDGE_SHIFT_M = 2.5
FR_HEIGHTS = {"limestone": .95, "dark_stone": .94, "brick": 1.05, "iron": 1.23, "mesh": 1.14,
              "wood": 1.16, "slats": 1.12, "rails": 1.19}
FR_PALETTES = {
    "alsace": ("wood", "mesh", "rails", "iron", "wood", "limestone"),
    "east": ("wood", "mesh", "iron", "rails", "limestone"),
    "north": ("brick", "brick", "rails", "mesh", "slats"),
    "loire": ("slats", "iron", "rails", "mesh", "slats", "limestone"),
    "southwest": ("iron", "mesh", "iron", "limestone", "wood"),
    "central": ("iron", "mesh", "wood", "rails", "dark_stone"),
    "brittany": ("mesh", "wood", "rails", "slats", "dark_stone"),
    "normandy": ("wood", "mesh", "limestone", "iron"),
    "paris_basin": ("iron", "rails", "limestone", "mesh"),
    "mediterranean": ("iron", "limestone", "slats", "mesh"),
    "alpine": ("wood", "rails", "dark_stone", "mesh"),
    "pyrenees": ("dark_stone", "wood", "iron", "mesh"),
    "basque": ("wood", "iron", "limestone", "mesh"),
    "toulouse": ("brick", "iron", "rails", "mesh"),
}
FR_WALLS = {"north": "brick", "toulouse": "brick", "central": "dark_stone", "alpine": "dark_stone",
            "brittany": "dark_stone", "pyrenees": "dark_stone"}



def _tags(feats, i):
    """source_tags of feature i as a lower-case dict, or {}."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()}


def kind_of(feats, i, tags=None):
    """'chainlink' or 'noisewall' when the mapped line is one, else None."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i) if tags is None else tags
    barrier = tags.get("barrier", "")
    fence_type = tags.get("fence_type") or tags.get("fence:type") or ""
    wall = tags.get("wall") or ""
    if cls in CHAIN_TYPES or barrier in CHAIN_TYPES or fence_type in CHAIN_TYPES:
        return G_CHAIN
    if cls in ("fence",) or barrier == "fence":
        return G_CHAIN if fence_type in CHAIN_TYPES else None
    if cls in NOISE_TYPES or barrier in NOISE_TYPES or wall in NOISE_TYPES:
        return G_WALL
    return None


def _yaw(dx, dy):
    """Degrees that turn a Forest item's local +x toward (dx, dy)."""
    return math.degrees(dmath.atan2(dy, dx)) % 360.0


def _centres(line, length):
    """(centre (2,), unit tangent) every `length` metres along a polyline, the first half a panel in."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return
    step = np.diff(line, axis=0)
    seglen = dnp.hypot(step[:, 0], step[:, 1])
    total = float(seglen.sum())
    if total < length * 0.5:
        return
    arc = np.concatenate(([0.0], np.cumsum(seglen)))
    s = length * 0.5
    while s <= total - length * 0.25:
        k = min(max(int(np.searchsorted(arc, s, side="right") - 1), 0), len(line) - 2)
        span = max(float(arc[k + 1] - arc[k]), 1e-9)
        t = (s - float(arc[k])) / span
        p = line[k] + t * (line[k + 1] - line[k])
        yield p[:2], step[k] / span
        s += length


def _place(group, centre, tangent):
    """(x, y, yaw) of one panel whose centre sits on the mapped line."""
    tx, ty = float(tangent[0]), float(tangent[1])
    cx, cy = float(centre[0]), float(centre[1])
    if group == G_CHAIN:
        return cx + CHAIN_OX * tx, cy + CHAIN_OX * ty, _yaw(tx, ty)
    # the noise wall's long axis is local +y, so local +x points to the right of the line
    return (cx - WALL_ACROSS * ty - WALL_ALONG * tx, cy + WALL_ACROSS * tx - WALL_ALONG * ty,
            _yaw(ty, -tx))


def _in_outlines(rings, pts):
    """Which points lie in one of the rings ((k, 2) bridge outlines) or within OUTLINE_PAD_M of its edge."""
    from polygon import contains_points
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    m = np.zeros(len(pts), bool)
    for ring in rings:
        m |= np.asarray(contains_points(ring, pts), bool)
        a, b = ring[:-1], ring[1:]
        d = b - a
        l2 = np.maximum(np.sum(d * d, axis=1), 1e-12)
        t = np.clip(np.sum((pts[:, None, :] - a[None]) * d[None], axis=2) / l2[None], 0.0, 1.0)
        near = a[None] + t[:, :, None] * d[None]
        m |= dnp.hypot(pts[:, None, 0] - near[:, :, 0], pts[:, None, 1] - near[:, :, 1]).min(axis=1) <= OUTLINE_PAD_M
    return m


def _project_polyline(pts, line):
    """Project XY points (n, 2) onto polyline `line` ((m, >=2)).
    Returns (dist (n,), closest_xy (n, 2), seg_tangent (n, 2), z_interp (n,) or None)."""
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    xy = line[:, :2]
    a, b = xy[:-1], xy[1:]
    d = b - a
    l2 = np.maximum((d * d).sum(1), 1e-12)
    v = pts[:, None, :] - a[None, :, :]
    t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
    q = a[None, :, :] + t[:, :, None] * d[None, :, :]
    dm = dnp.hypot(q[:, :, 0] - pts[:, None, 0], q[:, :, 1] - pts[:, None, 1])
    rr = np.arange(len(pts))
    k = np.argmin(dm, axis=1)
    seg = d[k] / np.maximum(dnp.hypot(d[k, 0], d[k, 1]), 1e-9)[:, None]
    zq = line[k, 2] + t[rr, k] * (line[k + 1, 2] - line[k, 2]) if line.shape[1] >= 3 else None
    return dm[rr, k], q[rr, k], seg, zq


def _on_road(points, z, roads):
    """Which XY points (at heights z) stand on a carriageway of roads (build_tile's (fid, spec, centreline (k, 3), _))
    at their own level (WALL_LEVEL_M): within its half width + WALL_ON_ROAD_M. Pure."""
    hit = np.zeros(len(points), bool)
    for r in roads:
        try:
            pts = np.asarray(r[2] if len(r) > 2 else r, np.float64)
            wide = float(getattr(r[1] if len(r) > 1 else None, "width", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if pts.ndim != 2 or pts.shape[1] < 3 or len(pts) < 2 or wide <= 0:
            continue
        dm, _q, _seg, zq = _project_polyline(points, pts)
        hit |= (dm < wide / 2.0 + WALL_ON_ROAD_M) & (np.abs(zq - z) <= WALL_LEVEL_M)
    return hit


def _along_deck(points, tangents, decks):
    """(distance from the nearest deck's centreline, its half width, |cos| of the angle between tangents and the
    deck there) for XY points (inf, 0, 0 with no deck within its half width + DECK_WIDE_REACH_M). Pure."""
    n = len(points)
    dist, half, cosa = np.full(n, np.inf), np.zeros(n), np.zeros(n)
    for deck in decks or ():
        line = np.asarray(deck.get("n", ()), np.float64)
        if line.ndim != 2 or len(line) < 2:
            continue
        dk, _q, seg, _zq = _project_polyline(points, line)
        h = float(deck.get("w", 0.0) or 0.0) / 2.0
        take = (dk < dist) & (dk <= h + DECK_WIDE_REACH_M)
        if take.any():
            c = np.abs((seg * tangents).sum(1))
            dist[take], half[take], cosa[take] = dk[take], h, c[take]
    return dist, half, cosa


# PR #505 (exencendre): French boundaries
def french_panel(cls, tags, region, fid):
    """Choose a model for an existing boundary. The regional mix fills missing appearance tags."""
    import furniture

    kind = tags.get("barrier") or cls
    if kind == "hedge":
        return "hedge"
    mat = tags.get("material", "")
    style = tags.get("fence_type") or tags.get("fence:type") or ""
    wall = tags.get("wall", "")
    if kind == "wall":
        if wall in {"retaining", "retaining_wall", "seawall", "flood_wall", "dyke", "castle_wall"}:
            return None
        if mat == "brick" or wall == "brick":
            return "brick"
        if mat in {"concrete", "plaster"}:
            return "limestone"
        return FR_WALLS.get(region, "limestone")
    if kind not in {"fence", *CHAIN_TYPES}:
        return None
    if mat == "wood" or style in {"wood", "wooden", "paling", "picket"}:
        return "wood"
    if mat in {"plastic", "pvc"} or style == "plastic":
        return "slats"
    if style in CHAIN_TYPES | {"wire", "wire_mesh", "welded_mesh", "mesh"} or kind in CHAIN_TYPES:
        return "mesh"
    if style in {"railings", "railing", "wrought_iron"} or mat in {"iron", "wrought_iron"}:
        return "iron"
    if mat in {"metal", "steel", "aluminium"}:
        return "rails"
    options = FR_PALETTES.get(region, FR_PALETTES["central"])
    return options[int(furniture._h01("fr-fence", fid) * len(options))]


def _panels(line, spacing=PANEL_M):
    """Centre, axis and length, fitting each segment without extending past its ends."""
    import roads
    line = roads.simplify(np.c_[line, np.zeros(len(line))], .08)[:, :2]
    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        length = float(dnp.norm(d))
        if length < .66:
            continue
        count = math.ceil(length / spacing)
        for i in range(count):
            yield a + d * ((i + .5) / count), d / length, length / count


def _french_panel_row(style, centre, direction, length):
    if style == "hedge":
        # The stock East Coast shrub has a broad crown. Use its height for a planted row and
        # overlap the foliage, so a mapped hedge reads continuously along bends and slopes.
        return G_HEDGE, *centre, _yaw(*direction), 1.8 + .18 * dmath.sin(float(centre.sum()) * .7), 0.
    return ("fr_fence_" + style, *centre, _yaw(*direction),
            FR_HEIGHTS[style] * length / PANEL_M, 0.)


def _french_panels(style, line):
    if style == "hedge":
        # Place along the whole arc instead of one shrub per short vertex-to-vertex span.
        return ((centre, tangent, HEDGE_STEP_M) for centre, tangent in _centres(line, HEDGE_STEP_M))
    return _panels(line)


def _forest_rows(pending, z):
    def drawn_yaw(group, x, y, yaw):
        if group == G_HEDGE:
            # Rotate the stock shrub independently of the row. The road clearance
            # is calculated beforehand with the unrotated tangent and a circular crown.
            seed = (round(x * 16) * 73856093) ^ (round(y * 16) * 19349663)
            yaw += seed % 360
        return round(yaw, 1) % 360.

    return [[group, round(x, 2), round(y, 2), round(float(zz) + lift, 2), drawn_yaw(group, x, y, yaw), 1., height, 0.]
            for (group, x, y, yaw, height, lift), zz in zip(pending, z)]


def _clear_hedges(rows, carriageways):
    """Leave room for the entire crown. Recede from a roadside edge, and leave crossings open."""
    import roads

    hedges = [i for i, r in enumerate(rows) if r[0] == G_HEDGE]
    if not hedges or not carriageways:
        return rows
    segments, starts, half = roads._blocker_segments(carriageways)
    if not len(segments):
        return rows
    a, b = segments[:, :2], segments[:, 2:]
    span = dnp.hypot(*(b - a).T)
    half = np.repeat(half, np.diff(starts))
    keep = span > 1e-6
    a, b, half = a[keep], b[keep], half[keep]
    span = span[keep]
    direction = (b - a) / span[:, None]
    reach = half[:, None] + HEDGE_SHIFT_M + 2.5
    low, high = np.minimum(a, b) - reach, np.maximum(a, b) + reach
    adjusted = list(rows)
    for i in hedges:
        group, x, y, yaw, height, lift = rows[i]
        p = np.array([x, y])
        near = np.all((p >= low) & (p <= high), axis=1)
        if not near.any():
            continue
        u, d, s, hw = a[near], direction[near], span[near], half[near]

        def distances(q):
            along = np.clip(np.sum((q - u) * d, axis=1), 0., s)
            delta = q - (u + along[:, None] * d)
            return delta, dnp.hypot(*delta.T)

        radius = height * HEDGE_CROWN_PER_M + HEDGE_ROAD_MARGIN_M
        delta, distance = distances(p)
        gaps = distance - hw
        j = int(gaps.argmin())
        if gaps[j] >= radius:
            continue
        tangent = np.array([dmath.cos(math.radians(yaw)), dmath.sin(math.radians(yaw))])
        move = radius - gaps[j] + .06
        if gaps[j] < -.45 or move > HEDGE_SHIFT_M or abs(dnp.matmul(d[j], tangent)) < .65:
            adjusted[i] = None
            continue
        p = p + move * delta[j] / max(distance[j], 1e-6)
        if np.any(distances(p)[1] - hw < radius):
            adjusted[i] = None
        else:
            adjusted[i] = (group, *p, yaw, height, lift)
    return adjusted


def _street_segments(carriageways):
    import europewalks
    import roads

    starts, ends, widths, local = [], [], [], []
    for _, spec, line, _ in carriageways:
        if spec.cls not in europewalks.STREETS or spec.kind != "paved" or spec.link or spec.median:
            continue
        line = roads.simplify(line, .12)[:, :2]
        if len(line) < 2:
            continue
        starts.extend(line[:-1])
        ends.extend(line[1:])
        widths.extend([spec.width / 2] * (len(line) - 1))
        local.extend([spec.cls in {"residential", "living_street", "unclassified"}] * (len(line) - 1))
    a, b = np.asarray(starts).reshape(-1, 2), np.asarray(ends).reshape(-1, 2)
    delta = b - a
    length = dnp.norm(delta, axis=1)
    keep = length > .01
    return a[keep], delta[keep] / length[keep, None], length[keep], np.asarray(widths)[keep], np.asarray(local)[keep]


def _street_front(ring, streets, door):
    """The nearest plausible street-facing wall, even when its house has no modelled door."""
    from buildings import _area2

    start, direction, length, half, local = streets
    ends = np.roll(ring, -1, axis=0)
    mid = (ring + ends) / 2
    edges = ends - ring
    widths = dnp.norm(edges, axis=1)
    along = edges / np.maximum(widths, 1e-9)[:, None]
    toward = np.c_[along[:, 1], -along[:, 0]] * (1 if _area2(ring) > 0 else -1)
    rel = mid[:, None, :] - start
    station = np.sum(rel * direction, axis=2)
    q = start + np.clip(station, 0., length)[..., None] * direction
    delta = mid[:, None, :] - q
    gap = dnp.norm(delta, axis=2)
    setback = gap - half
    faces = (np.sum(delta * toward[:, None, :], axis=2) <= -.57 * gap)
    parallel = np.abs(dnp.matmul(along, direction.T)) >= .58
    # A parallel street ending well before the wall is not its frontage.
    abreast = (station >= -2.5) & (station <= length + 2.5)
    eligible = faces & parallel & abreast & (setback >= 1.7) & (setback <= 22.) & (widths[:, None] >= 3.8)
    if not eligible.any():
        return None
    rank = np.where(eligible, setback - .45 * local, np.inf)
    if door is not None and door[0] < len(ring):
        rank[door[0]] -= 1.1
    edge, road = np.unravel_index(rank.argmin(), rank.shape)
    tangent = direction[road] * (1 if dnp.matmul(direction[road], along[edge]) > 0 else -1)
    return (edge, mid[edge], along[edge], widths[edge], q[edge, road], delta[edge, road] / gap[edge, road],
            tangent, half[road], setback[edge, road], local[road])


def inferred_rows(frame, houses, rings, walks, carriageways, barriers, paved, green, drives, z_at, photo, *,
                  residential=(), public=(), driveways=()):
    """Supplement mapped French boundaries at suitable residential setbacks."""
    import furniture
    import roads
    import streetfront
    from francehouses import HOUSE_CLASSES
    from house_regions import profile

    streets = _street_segments(carriageways)
    if not houses or not len(streets[0]):
        return []
    road_bands = [(road[:, :2], spec.width / 2) for _, spec, road, _ in carriageways]
    road_blocks = roads._blocker_segments([(line, half + .30) for line, half in road_bands])
    access = [(road[:, :2], spec.width / 2 + .3) for _, spec, road, _ in carriageways if spec.cls == "service"]
    access.extend((np.asarray(d["n"])[:, :2], d["w"] / 2 + .3) for d in drives)
    access.extend((road[:, :2], spec.width / 2 + .3) for _, spec, road in driveways)
    access_blocks = roads._blocker_segments(access)
    footways = [(np.asarray(w["pts"]), w["w"] / 2) for w in walks if "pts" in w]
    foot_blocks = roads._blocker_segments([(p[:, :2], hw + .15) for p, hw in footways])
    walk_edges, corners = [], []
    for walk in walks:
        if "pts" in walk or "corner" in walk:
            curb, back = streetfront._walk_outline(walk)
            walk_edges.extend(line for line in (curb, back) if len(line) >= 2)
            if "corner" in walk:
                corners.append(np.concatenate([curb, back[::-1]]))
    walk_walls = streetfront._walls([], walk_edges)
    blocked_ground = [*paved, *public]
    exclusion = streetfront._walls(blocked_ground, barriers)
    building_walls = streetfront._walls(rings)
    corner_walls = streetfront._walls(corners)
    pending = []
    for spec, ring in houses:
        seed = spec.seed
        chance = furniture._h01("fr-yard-fence", seed)
        if chance >= .9:
            continue
        door = getattr(spec, "door", None)
        front = _street_front(ring, streets, door)
        if front is None:
            continue
        edge, mid, along, width, q, out, tangent, half, setback, local = front
        a, b = ring[edge], ring[(edge + 1) % len(ring)]
        offset = half + 1.
        curb = q + (half - .15) * out
        first = walk_walls.ray(curb, out, min(4., setback))
        if first is not None:
            second = walk_walls.ray(curb + (first + .04) * out, out, min(4.9, setback - first + .1))
            edge_of_walk = first if second is None else first + .04 + second
            offset = max(offset, half - .15 + edge_of_walk + .53)
        depth = setback + half - offset
        if not 1. <= depth <= 18.:
            continue
        centre = q + offset * out
        hidden = building_walls.ray(centre, out, depth - .45)
        if hidden is not None:
            continue
        length = min(width * abs(dnp.matmul(along, tangent)) - .4, 20.)
        ends = np.array([centre - tangent * length / 2, centre + tangent * length / 2])
        near_wall = np.array([a + .18 * (mid - a), b + .18 * (mid - b)]) - .22 * out
        yard = np.array([*ends, near_wall[1], near_wall[0]])[None]
        if (streetfront._green_overlap(yard, blocked_ground, exclusion, barriers)[0]
                or streetfront._road_overlap(yard, access_blocks)[0]):
            continue
        yard_centre = np.array([(centre + mid) * .5])
        garden = streetfront._inside(yard_centre, green)[0]
        if photo is not None:
            garden |= photo.strip_share(yard, photo.green)[0] >= .23
            if not garden and photo.strip_share(yard, photo.mask)[0] >= .71:
                continue
        if not garden:
            known_residential = streetfront._inside(yard_centre, residential)[0]
            likely_home = getattr(spec, "cls", None) in HOUSE_CLASSES or getattr(spec, "roof", None) != "flat"
            if known_residential or local:
                minimum, density = (1.15, .84) if likely_home else (1.7, .66)
            else:
                minimum, density = (1.7, .71) if likely_home else (2.7, .46)
            if depth < minimum or chance >= density:
                continue
        # The boundary itself must clear curbs, footways, existing barriers and nearby walls.
        strip = np.array([ends[0] - .36 * out, ends[1] - .36 * out,
                          ends[1] + .36 * out, ends[0] + .36 * out])[None]
        if (streetfront._road_overlap(strip, road_blocks)[0] or streetfront._road_overlap(strip, foot_blocks)[0]
                or streetfront._green_overlap(strip, rings, building_walls)[0]
                or streetfront._green_overlap(strip, blocked_ground, exclusion, barriers)[0]
                or corners and streetfront._green_overlap(strip, corners, corner_walls)[0]):
            continue
        lat, lon, _ = frame.enu_to_geodetic(*centre, 0.)
        region = profile(float(lat), float(lon))
        if region is None:
            continue
        style = ("hedge" if garden and depth > 3. and furniture._h01("fr-yard-hedge", seed) < .20
                 else french_panel("fence", {}, region, seed))
        if door is not None and door[0] == edge:
            entry = float(dnp.matmul(a + door[1] * along - centre, tangent))
        else:
            entry = length * (.32 if furniture._h01("fr-yard-entry", seed) < .5 else -.32)
        # An open gate stays wide enough for a car, with clearance for the shrub's crown.
        gap_half = 2.2 if style == "hedge" else 1.6
        for start, finish in ((-length / 2, entry - gap_half), (entry + gap_half, length / 2)):
            start, finish = max(start, -length / 2), min(finish, length / 2)
            if finish - start >= (.9 if style == "hedge" else .72):
                line = np.array([centre + start * tangent, centre + finish * tangent])
                pending.extend(_french_panel_row(style, p, d, panel_m)
                               for p, d, panel_m in _french_panels(style, line))
    pending = [r for r in _clear_hedges(pending, road_bands + access) if r is not None]
    if not pending:
        return []
    x, y = np.array([(r[1], r[2]) for r in pending]).T
    return _forest_rows(pending, z_at(x, y))



def french_rows(frame, infra_fn, box, elev, carriageways=()):
    """(rows, fids) of PR #505's French mapped boundaries in box: fences (chain-link ones too), garden walls and hedges
    as regional panels / the stock shrub (french_panel), clear of the carriageways and of mapped gates; fids: the
    features drawn so (rows() leaves them out). Noise walls, the stone walls stone_walls draws and anything on a bridge
    (its tags or Overture's level) are left to the other passes."""
    if infra_fn is None:
        return [], set()
    import geotiles
    from house_regions import profile
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return [], set()
    fr_pending, gates, taken, seen = [], [], set(), set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("fences: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        nearby = ((boxes[:, 2] >= x0 - BBOX_PAD_M) & (boxes[:, 0] <= x1 + BBOX_PAD_M) &
                  (boxes[:, 3] >= y0 - BBOX_PAD_M) & (boxes[:, 1] <= y1 + BBOX_PAD_M))
        classes = feats.attrs.get("class")
        for i in np.flatnonzero(nearby):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            cls = str(classes[i]).strip().lower() if classes is not None else ""
            if feats.kind[i] == mapgeom.POINT and cls in {"gate", "lift_gate", "swing_gate"}:
                gates.append(boxes[i, :2])
                continue
            if feats.kind[i] not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            tags = _tags(feats, i)
            if not (cls in {"fence", "wall", "hedge", *CHAIN_TYPES} or tags.get("barrier") in {"fence", "wall", "hedge"}):
                continue
            if kind_of(feats, i, tags) == G_WALL or (STONE_ON and stone_height(feats, i) is not None):
                continue
            if _deck_level_hint(feats, i):
                continue
            try:
                lv = float(np.asarray(feats.attrs.get("level"))[i])
            except (TypeError, ValueError, IndexError):
                lv = 0.0
            if lv > 0:
                continue
            la_, lo_, _ = frame.enu_to_geodetic(*boxes[i].reshape(2, 2).mean(axis=0), 0.)
            region = profile(float(la_), float(lo_))
            if region is None:
                continue
            style = french_panel(cls, tags, region, fid)
            heights = feats.attrs.get("height")
            if style is not None and style != "hedge" and heights is not None and heights[i] is not None:
                try:
                    if float(heights[i]) > 2.3:
                        style = None
                except (TypeError, ValueError):
                    pass
            if style is None:
                continue
            taken.add(fid)
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                for centre, tangent, length_m in _french_panels(style, np.column_stack((e, n))):
                    if geotiles.box_owns(box, *centre):
                        fr_pending.append((style, centre, tangent, length_m))
    if not fr_pending:
        return [], taken
    import roads
    import streetfront
    carriageways = list(carriageways or ())
    centres = np.array([row[1] for row in fr_pending])
    offsets = np.array([row[2] * row[3] / 2 for row in fr_pending])
    if carriageways:
        bounds = roads._blocker_segments([(np.asarray(line)[:, :2], max(.3, spec.width / 2 - .1))
                                          for _, spec, line, *_ in carriageways])
        cross = streetfront._road_overlap(np.stack((centres - offsets, centres + offsets), axis=1), bounds)
    else:
        cross = np.zeros(len(centres), bool)
    drawn = [_french_panel_row(style, centre, direction, length) for style, centre, direction, length in fr_pending]
    bands = [(np.asarray(line)[:, :2], spec.width / 2) for _, spec, line, *_ in carriageways]
    pending = []
    for (style, _, _, length), bad, row in zip(fr_pending, cross, _clear_hedges(drawn, bands)):
        if row is None or (bad and style != "hedge"):
            continue
        clearance = (row[4] * HEDGE_CROWN_PER_M + .75) if style == "hedge" else 1.3 + length / 2
        if any(dnp.norm(np.array(row[1:3]) - gate) < clearance for gate in gates):
            continue
        pending.append(row)
    if not pending:
        return [], taken
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return _forest_rows(pending, z), taken


def _infra_cells(frame, box):
    """Infrastructure cell keys overlapping `box` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        return mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []


def _fit_inputs(roads, decks):
    """Build carriageway polyline tuples `(fits, deck_fits)` from `roads` and `decks`."""
    fits = []
    deck_fits = []
    for r in roads or ():
        try:
            pts = np.asarray(r[2] if len(r) > 2 else r, np.float64)
            spec = r[1] if len(r) > 1 else None
        except (TypeError, ValueError):
            continue
        if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
            try:
                wide = float(getattr(spec, "width", 0.0) or 0.0)
            except (TypeError, ValueError):
                wide = 0.0
            if wide > 0:
                fits.append((pts, wide / 2.0, False))
    for d in decks or ():
        pts = np.asarray(d.get("n", ()), np.float64)
        if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
            try:
                wide = float(d.get("w", 0.0) or 0.0)
            except (TypeError, ValueError):
                wide = 0.0
            if wide > 0:
                fits.append((pts, wide / 2.0, True))
                deck_fits.append((pts, wide / 2.0))
    return fits, deck_fits


def _bridge_hint(feats, i, has_decks):
    """Return True (bridge), False (under), or None (unknown) for infrastructure feature `i`."""
    if not has_decks:
        return None
    hint = _deck_level_hint(feats, i)
    if hint is not None:
        return hint
    # Overture keeps a way's OSM layer in the feature's own level, not always in its tags
    # (rail-viaduct noise walls seen with tags without layer, level=2.0): that counts as on a bridge.
    try:
        lv = float(np.asarray(feats.attrs.get("level"))[i])
    except (TypeError, ValueError, IndexError):
        lv = float("nan")
    if lv == lv:
        return True if lv > 0 else (False if lv < 0 else None)
    return None


def _collect_wall_features(frame, infra_fn, box, has_decks, allowed_groups, seen_init=()):
    """Collect matching barrier features and bridge outlines intersecting `box`.
    Returns `(items, outlines)` where each item is `(group, fid, hint, parts_enu)`."""
    x0, y0, x1, y1 = map(float, box)
    items, outlines = [], []
    seen, seen_outlines = set(seen_init), set()
    for key in _infra_cells(frame, box):
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("fences: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        classes = feats.attrs.get("class")
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            if (has_decks and int(feats.kind[i]) == mapgeom.POLYGON and classes is not None
                    and str(classes[i]).strip().lower() == "bridge" and fid not in seen_outlines):
                seen_outlines.add(fid)
                for part in feats.parts(i):
                    if part and len(part[0]) >= 4:
                        e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                        outlines.append(np.column_stack((e, n)))
                continue
            group = kind_of(feats, i)
            if group not in allowed_groups:
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            seen.add(fid)
            hint = _bridge_hint(feats, i, has_decks)
            parts_enu = []
            for part in feats.parts(i):
                if part and len(part[0]) >= 2:
                    e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                    parts_enu.append(np.column_stack((e, n)))
            if parts_enu:
                items.append((group, fid, hint, parts_enu))
    return items, outlines


def _fit_to_carriageways(mid, wt, on_bridge, untagged, ref, fits, deck_fits, inside):
    """Snap wall sample points onto nearby parallel carriageways and detect across-road samples."""
    n = len(mid)
    par_min = dmath.cos(math.radians(WALL_FIT_MAX_DEG))
    ref = ref.copy()
    if deck_fits and on_bridge.any():
        dd = np.full(n, np.inf)
        dz = np.zeros(n)
        for line, _half in deck_fits:
            dm, _q, _seg, zq = _project_polyline(mid, line)
            zq = ref if zq is None else zq
            take = dm < dd
            dd[take] = dm[take]
            dz[take] = zq[take]
        on = on_bridge & np.isfinite(dd) & (dd <= WALL_FIT_REACH_M)
        ref = np.where(on, dz, ref)
    if untagged.any():
        rd = np.full(n, np.inf)
        rz = np.zeros(n)
        for line, _half, is_deck in fits:
            if is_deck:
                continue
            dm, _q, seg, zq = _project_polyline(mid, line)
            zq = ref if zq is None else zq
            along = np.abs((wt * seg).sum(1))
            take = (dm < rd) & (along >= par_min) & (dm <= WALL_FIT_REACH_M)
            rd[take] = dm[take]
            rz[take] = zq[take]
        ref = np.where(untagged & np.isfinite(rd), rz, ref)
    fd = np.full(n, np.inf)
    ft = np.zeros((n, 2))
    fh = np.zeros(n)
    pd = np.full(n, np.inf)
    pv = np.zeros((n, 2))
    pt = np.zeros((n, 2))
    ph = np.zeros(n)
    pz = np.zeros(n)
    pfi = np.full(n, -1, np.int64)
    for fi, (line, half, is_deck) in enumerate(fits):
        dm, q, seg, zq = _project_polyline(mid, line)
        zq = ref if zq is None else zq
        lev = np.abs(zq - ref) <= WALL_LEVEL_M
        plan = ((dm <= half + WALL_DECK_REACH_M) | (inside if inside is not None else False)) if is_deck else np.ones(n, bool)
        take = (dm < fd) & lev & plan
        if take.any():
            fd[take] = dm[take]
            ft[take] = seg[take]
            fh[take] = half
        along = np.abs((wt * seg).sum(1))
        take = (dm < pd) & (along >= par_min) & lev & plan
        if take.any():
            pd[take] = dm[take]
            pv[take] = q[take] - mid[take]
            pt[take] = seg[take]
            ph[take] = half
            pz[take] = zq[take]
            pfi[take] = fi
    near = np.isfinite(pd) & (pd <= WALL_FIT_REACH_M)
    across = np.isfinite(fd) & (fd <= WALL_FIT_REACH_M) & (fd <= fh + 1.0) \
        & (np.abs((wt * ft).sum(1)) < dmath.cos(math.radians(WALL_ACROSS_DEG)))
    return ref, near, pv, pt, ph, pz, pfi, across


def _apply_deck_continuity(groups_vals, mid, tang, z, wide, ground, decks, roads):
    """Snap untagged underpass-spanning runs and parapet runs onto bridge deck elevation in-place."""
    for idx in groups_vals:
        if len(idx) < 3:
            continue
        idx = np.asarray(idx, np.int64)
        near_deck = np.isfinite(wide[idx])
        at_deck = near_deck & (np.abs(wide[idx] - ground[idx]) <= DECK_AUTO_SNAP_TOLERANCE_M)
        off_deck = near_deck & ~at_deck
        i0 = 0
        while i0 < len(idx):
            if not off_deck[i0]:
                i0 += 1
                continue
            i1 = i0
            while i1 + 1 < len(idx) and off_deck[i1 + 1]:
                i1 += 1
            before = at_deck[i0 - 1] if i0 > 0 else None
            after = at_deck[i1 + 1] if i1 + 1 < len(idx) else None
            if (before is None or before) and (after is None or after) and (before or after):
                z[idx[i0:i1 + 1]] = wide[idx[i0:i1 + 1]]
            i0 = i1 + 1
    for idx in groups_vals if PARAPET_ON else ():
        idx = np.asarray(idx, np.int64)
        dist, half, cosa = _along_deck(mid[idx], tang[idx], decks)
        along = np.isfinite(dist) & (dist <= half + ALONG_EDGE_M) & (cosa >= dmath.cos(math.radians(ALONG_DEG))) \
            & np.isfinite(wide[idx])
        if along.sum() < max(2, ALONG_FRAC * len(idx)):
            continue
        if not roads or not _on_road(mid[idx[along]], z[idx[along]], roads).any():
            continue
        z[idx[along]] = wide[idx[along]]


def rows(frame, infra_fn, box, elev, decks=(), roads=(), french=False, carriageways=None,
         include_noisewalls=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the chain-link fences and noise walls whose
    centre is inside box (x0, y0, x1, y1). Pass include_noisewalls=False when continuous noise_walls() is built
    into roadmesh.tunnel instead."""
    if infra_fn is None:
        return []
    if carriageways is not None and not roads:      # (PR #505's name for the same roads_out rows)
        roads = carriageways
    fr_rows, fr_taken = french_rows(frame, infra_fn, box, elev, roads) if french else ([], set())
    x0, y0, x1, y1 = map(float, box)
    meshed_noise = _get_meshed_noise(frame, box) if include_noisewalls is None else set()
    allow_walls = True if include_noisewalls is None else bool(include_noisewalls)
    allowed = (G_CHAIN, G_WALL) if allow_walls else (G_CHAIN,)
    items, outlines = _collect_wall_features(frame, infra_fn, box, bool(decks), allowed, seen_init=fr_taken)
    if meshed_noise:
        items = [it for it in items if not (it[0] == G_WALL and it[1] in meshed_noise)]
    pending = []
    for group, fid, hint, parts_enu in items:
        length = CHAIN_LEN if group == G_CHAIN else WALL_LEN
        for pi, xy_part in enumerate(parts_enu):
            for centre, tangent in _centres(xy_part, length):
                px, py, yaw = _place(group, centre, tangent)
                keep = x0 <= centre[0] < x1 and y0 <= centre[1] < y1
                pending.append((group, px, py, yaw, float(centre[0]), float(centre[1]), hint, fid, pi,
                                keep, float(tangent[0]), float(tangent[1])))
    if not pending:
        return fr_rows
    near = np.zeros(len(pending), bool)     # (set below: walls laid along a carriageway)
    pz = np.zeros(len(pending))             # (that carriageway's own height there)
    walls = np.array([p[0] == G_WALL for p in pending], bool)
    if (roads or decks) and walls.any():
        fits, deck_fits = _fit_inputs(roads, decks)
        if fits:
            mid = np.array([[p[4], p[5]] for p in pending], np.float64)
            ang = np.radians([p[3] for p in pending])
            wt = np.stack([-dnp.sin(ang), dnp.cos(ang)], axis=1)
            inside = _in_outlines(outlines, mid) if outlines else None
            la, lo, _ = frame.enu_to_geodetic(mid[:, 0], mid[:, 1], np.zeros(len(mid)))
            ref = np.asarray(elev(la, lo), np.float64).reshape(-1)
            on_bridge = np.array([p[6] is True for p in pending], bool)
            untagged = np.array([p[0] == G_WALL and p[6] is None for p in pending], bool)
            ref, near_fit, pv, pt, ph, pz_fit, _pfi, across_fit = _fit_to_carriageways(
                mid, wt, on_bridge, untagged, ref, fits, deck_fits, inside)
            near = walls & near_fit
            pz = np.where(near, pz_fit, 0.0)
            for i in np.flatnonzero(near).tolist():
                p = pending[i]
                T = pt[i]
                left = np.array([-T[1], T[0]])
                side = 1.0 if float((pv[i] * left).sum()) < 0.0 else -1.0
                here = mid[i] + pv[i] + side * left * (ph[i] + WALL_EDGE_M + WALL_ACROSS)
                t_new = side * T
                px, py, yaw = _place(p[0], (float(here[0]), float(here[1])),
                                     (float(t_new[0]), float(t_new[1])))
                keep = x0 <= here[0] < x1 and y0 <= here[1] < y1
                pending[i] = ((p[0], px, py, yaw, float(here[0]), float(here[1])) + p[6:9]
                              + (keep, float(t_new[0]), float(t_new[1])))
            for i in np.flatnonzero(walls & across_fit).tolist():
                p = pending[i]
                pending[i] = p[:9] + (False,) + p[10:]
            keep_ix = [i for i, p in enumerate(pending) if p[0] == G_WALL and p[9]]
            if keep_ix:
                kp = np.array([[pending[i][4], pending[i][5]] for i in keep_ix], np.float64)
                kt = np.array([[pending[i][10], pending[i][11]] for i in keep_ix], np.float64)
                probes = (kp, kp + kt * (WALL_LEN / 2.0), kp - kt * (WALL_LEN / 2.0))
                kref = ref[keep_ix]
                on = np.zeros(len(keep_ix), bool)
                for line, half, is_deck in fits:
                    if is_deck:
                        continue
                    for pt_probe in probes:
                        dm, _q, _seg, zq = _project_polyline(pt_probe, line)
                        lev = (np.abs(zq - kref) <= WALL_LEVEL_M) if zq is not None else np.ones(len(pt_probe), bool)
                        on |= (dm < half + WALL_ON_ROAD_M) & lev
                for j in np.flatnonzero(on).tolist():
                    p = pending[keep_ix[j]]
                    pending[keep_ix[j]] = p[:9] + (False,) + p[10:]
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    if decks:
        mid = np.array([[p[4], p[5]] for p in pending], np.float64)
        surf, ground, wide = _surface_info(frame, mid, elev, decks, [p[6] for p in pending])
        on = np.abs(surf - ground) > 1e-6
        if outlines:
            edge = ~on & np.isfinite(wide) & np.array([p[6] is True for p in pending])
            if edge.any():
                edge[edge] = _in_outlines(outlines, mid[edge])
            surf, on = np.where(edge, wide, surf), on | edge
        z = np.where(on, surf, z)
        groups = {}
        for i, p in enumerate(pending):
            if p[6] is None:
                groups.setdefault((p[7], p[8]), []).append(i)
        tang = np.array([[p[10], p[11]] for p in pending], np.float64)
        _apply_deck_continuity(list(groups.values()), mid, tang, z, wide, ground, decks, roads)
    if walls.any():
        z = np.where(near, pz, z)
    chain_on = [i for i, p in enumerate(pending) if p[0] == G_CHAIN and p[9]]
    if chain_on and roads and CHAIN_OFF_ROAD_ON:
        cxy = np.array([[pending[i][4], pending[i][5]] for i in chain_on], np.float64)
        for j in np.flatnonzero(_on_road(cxy, z[chain_on], roads)).tolist():
            p = pending[chain_on[j]]
            pending[chain_on[j]] = p[:9] + (False,) + p[10:]
    out = list(fr_rows)
    for p, zz in zip(pending, z):
        if not p[9]:
            continue
        group, px, py, yaw = p[0], p[1], p[2], p[3]
        lift = CHAIN_LIFT if group == G_CHAIN else 0.0
        height = CHAIN_H if group == G_CHAIN else WALL_H
        out.append([group, round(px, 2), round(py, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0,
                    1.0, height, 0.0])
    return out


# Mapped stone walls (GitHub #938: the field walls along Ireland's and Britain's country roads). OSM's barrier=wall with
# wall=dry_stone (or another stone wall type, or material=stone and no wall=), drawn as a stone solid along the mapped
# line: STONE_THICK_M thick, its own height= tag within STONE_H_RANGE else STONE_H_M over the ground, its foot
# STONE_FOOT_M into it (the ground between the line's points is not flat). The retaining walls' mesh draws it
# (roadmesh._retaining_walls, a parapet: stone on both faces down to its foot). Left out: a wall on a bridge (its
# bridge / layer tags or Overture's level), and the metres of it within STONE_ROAD_GAP_M of a ground road's edge (a
# wall mapped to the road's centre line at a gate or a junction would block it). BNE_STONE_WALLS=off: none.
STONE_ON = os.environ.get("BNE_STONE_WALLS", "on") != "off"
STONE_TYPES = {"dry_stone", "drystone", "dry_stone_wall", "stone", "field_stone", "fieldstone", "flint", "rubble",
               "stone_wall"}
STONE_MATERIALS = {"stone", "dry_stone", "granite", "limestone", "sandstone", "slate", "flint"}
STONE_H_M = 1.1
STONE_H_RANGE = (0.4, 2.5)
STONE_THICK_M = 0.5         # (roadmesh.WALL_COPING_M: all of its top is stone, no strip of soil)
STONE_FOOT_M = 0.3
STONE_STEP_M = 1.0
STONE_ROAD_GAP_M = 0.6


def stone_height(feats, i):
    """The height (m) of mapped stone wall i, or None when it is not one (STONE_TYPES / STONE_MATERIALS)."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i)
    if tags.get("barrier", cls) != "wall" and cls != "wall":
        return None
    wall = tags.get("wall", "")
    if not (wall in STONE_TYPES or (not wall and tags.get("material", "") in STONE_MATERIALS)):
        return None
    try:                            # Overture's height, else the OSM tag
        h = float(np.asarray(feats.attrs.get("height"))[i])
    except (TypeError, ValueError, IndexError):
        h = float("nan")
    if not h == h:
        try:
            h = float(tags.get("height", "").replace("m", "").strip())
        except ValueError:
            h = float("nan")
    return h if STONE_H_RANGE[0] <= h <= STONE_H_RANGE[1] else STONE_H_M


def _resample(line, step):
    """Points every ~step m along a polyline, its own end points kept."""
    seg = dnp.hypot(*np.diff(line, axis=0).T)
    arc = np.concatenate(([0.0], np.cumsum(seg)))
    if arc[-1] < 1e-6:
        return line[:1]
    n = max(int(math.ceil(arc[-1] / step)), 1)
    s = np.linspace(0.0, arc[-1], n + 1)
    return np.column_stack((dnp.interp(s, arc, line[:, 0]), dnp.interp(s, arc, line[:, 1])))


def _near_road(xy, roads):
    """(n,) bool: the points within a ground road's half width + STONE_ROAD_GAP_M of its centre line."""
    hit = np.zeros(len(xy), bool)
    lo, hi = xy.min(0), xy.max(0)
    for _fid, spec, pts, *_r in roads:
        line = np.asarray(pts, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = float(getattr(spec, "width", 0.0) or 0.0) / 2 + STONE_ROAD_GAP_M
        if (line.min(0) > hi + reach).any() or (line.max(0) < lo - reach).any():
            continue
        dm, _q, _seg, _zq = _project_polyline(xy, line)
        hit |= dm < reach
    return hit


def _smooth_baffle_jogs(xy):
    """Collapse short sharp zig-zag acoustic-baffle jogs while preserving real corners and gentle curves."""
    xy = np.asarray(xy, np.float64)
    if len(xy) < 2:
        return xy
    pts = [xy[0]]
    for pt in xy[1:]:
        if float(dnp.hypot(*(pt - pts[-1]))) > 1e-4:
            pts.append(pt)
    changed = True
    while changed and len(pts) >= 4:
        changed = False
        i = 1
        while i <= len(pts) - 3:
            d_in = pts[i] - pts[i - 1]
            l_in = float(dnp.hypot(*d_in))
            d_entry = pts[i + 1] - pts[i]
            l_entry = float(dnp.hypot(*d_entry))
            if l_in < 1.0 or l_entry < 1e-6:
                i += 1
                continue
            u_in = d_in / l_in
            u_entry = d_entry / l_entry
            if float(dnp.dot(u_in, u_entry)) >= 0.85:
                i += 1
                continue
            path_len = l_entry
            collapsed = False
            for j in range(i + 2, min(len(pts) - 1, i + 5)):
                step_j = pts[j] - pts[j - 1]
                l_step = float(dnp.hypot(*step_j))
                path_len += l_step
                if path_len > 3.0 * NOISE_JOG_MAX_M:
                    break
                d_out = pts[j + 1] - pts[j]
                l_out = float(dnp.hypot(*d_out))
                if l_out < 1.0 or l_step < 1e-6:
                    continue
                u_out = d_out / l_out
                u_exit = step_j / l_step
                if float(dnp.dot(u_in, u_out)) < NOISE_JOG_COS_MIN or float(dnp.dot(u_exit, u_out)) >= 0.85:
                    continue
                ch = pts[j] - pts[i]
                ch_len = float(dnp.hypot(*ch))
                if ch_len < 1e-3 or ch_len > 3.0 * NOISE_JOG_MAX_M:
                    continue
                u_ch = ch / ch_len
                if float(dnp.dot(u_ch, u_in)) < NOISE_JOG_COS_MIN or float(dnp.dot(u_ch, u_out)) < NOISE_JOG_COS_MIN:
                    continue
                n_ch = np.array([-u_ch[1], u_ch[0]])
                rel = np.asarray(pts[i + 1:j]) - pts[i]
                perp = np.abs(dnp.matmul(rel, n_ch))
                along = dnp.matmul(rel, u_ch)
                if np.all((perp <= NOISE_JOG_MAX_M) & (along >= -NOISE_JOG_MAX_M) & (along <= ch_len + NOISE_JOG_MAX_M)):
                    pts = pts[:i + 1] + pts[j:]
                    changed = True
                    collapsed = True
                    break
            if not collapsed:
                i += 1
    return np.asarray(pts, np.float64)


def _bridge_fit_gaps(xy, ref, fits, inside, near, pv, pt, ph, pz, pfi, p_side, across):
    """Snap short interior non-parallel kinks (e.g. shifted baffle steps) onto an enclosing carriageway fit."""
    i0 = 0
    n = len(xy)
    while i0 < n:
        if near[i0]:
            i0 += 1
            continue
        i1 = i0
        while i1 + 1 < n and not near[i1 + 1]:
            i1 += 1
        if (i0 > 0 and i1 + 1 < n and pfi[i0 - 1] >= 0
                and pfi[i0 - 1] == pfi[i1 + 1] and p_side[i0 - 1] == p_side[i1 + 1]):
            fi = int(pfi[i0 - 1])
            side0 = float(p_side[i0 - 1])
            arc = float(dnp.hypot(*np.diff(xy[i0 - 1:i1 + 2], axis=0).T).sum())
            if arc <= NOISE_GAP_SNAP_M:
                line, half, is_deck = fits[fi]
                sub = xy[i0:i1 + 1]
                dm, q, seg, zq = _project_polyline(sub, line)
                zq = ref[i0:i1 + 1] if zq is None else zq
                lev = np.abs(zq - ref[i0:i1 + 1]) <= WALL_LEVEL_M
                plan = (((dm <= half + WALL_DECK_REACH_M) | (inside[i0:i1 + 1] if inside is not None else False))
                        if is_deck else np.ones(len(sub), bool))
                left = np.column_stack((-seg[:, 1], seg[:, 0]))
                q_pv = q - sub
                q_side = np.where((q_pv * left).sum(1) < 0.0, 1.0, -1.0)
                if np.all((dm <= WALL_FIT_REACH_M) & lev & plan & (q_side == side0)):
                    near[i0:i1 + 1] = True
                    pv[i0:i1 + 1] = q_pv
                    pt[i0:i1 + 1] = seg
                    ph[i0:i1 + 1] = half
                    pz[i0:i1 + 1] = zq
                    pfi[i0:i1 + 1] = fi
                    p_side[i0:i1 + 1] = side0
                    across[i0:i1 + 1] = False
        i0 = i1 + 1


def _wall_runs_from_mask(base, toward, top, foot, valid, box):
    """Split vertex arrays into contiguous kept wall dicts whose segment midpoints lie in `box`."""
    x0, y0, x1, y1 = map(float, box)
    mid_seg = 0.5 * (base[:-1] + base[1:])
    step_ok = dnp.hypot(*(base[1:] - base[:-1]).T) <= 4.0 * NOISE_STEP_M
    tw_ok = (toward[:-1] * toward[1:]).sum(1) >= 0.0
    keep = (valid[:-1] & valid[1:] & step_ok & tw_ok
            & (mid_seg[:, 0] >= x0) & (mid_seg[:, 0] < x1)
            & (mid_seg[:, 1] >= y0) & (mid_seg[:, 1] < y1))
    out = []
    j = 0
    while j < len(keep):
        if not keep[j]:
            j += 1
            continue
        k = j
        while k < len(keep) and keep[k]:
            k += 1
        sub_b = base[j:k + 1]
        if len(sub_b) >= 2 and float(dnp.hypot(*np.diff(sub_b, axis=0).T).sum()) >= 0.1:
            sub_top = top[j:k + 1]
            out.append({
                "base": sub_b,
                "toward": toward[j:k + 1],
                "top": sub_top,
                "back_top": sub_top.copy(),
                "foot": foot[j:k + 1],
                "thick": np.full(len(sub_b), NOISE_THICK_M),
                "parapet": True,
                "concrete": True,
                "mountain": False,
                "rock": False,
            })
        j = k
    return out


def noise_walls(frame, infra_fn, box, elev, decks=(), roads=(), carriageways=None,
                only_bridge=None, meshed_out=None, bridge_seen_out=None):
    """Mapped noise barriers in box (x0, y0, x1, y1) as continuous grade-following retaining_walls dicts
    for roadmesh.tunnel's concrete: smoothed over short baffle jogs, snapped onto adjacent carriageways,
    carried cleanly onto bridges, cut across ground roads, and split seamlessly at tile borders."""
    if infra_fn is None or not NOISE_ON:
        return []
    if carriageways is not None and not roads:
        roads = carriageways
    has_decks = bool(decks) or (only_bridge is not None)
    items, outlines = _collect_wall_features(frame, infra_fn, box, has_decks, (G_WALL,))
    if not items:
        return []
    if bridge_seen_out is not None and any(it[2] is True for it in items):
        bridge_seen_out.append(True)
    if only_bridge is False:
        items = [it for it in items if it[2] is not True]
    elif only_bridge is True:
        items = [it for it in items if it[2] is True]
    if not items:
        return []
    fits, deck_fits = _fit_inputs(roads, decks)
    part_recs = []
    all_xy = []
    for _group, fid, hint, parts_enu in items:
        for pi, xy_part in enumerate(parts_enu):
            xy = _resample(_smooth_baffle_jogs(xy_part), NOISE_STEP_M)
            if len(xy) < 2:
                continue
            tang = np.gradient(xy, axis=0)
            tang /= np.maximum(dnp.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
            part_recs.append((fid, pi, hint, xy, tang))
            all_xy.append(xy)
    if not part_recs:
        return []
    all_xy_cat = np.concatenate(all_xy)
    la, lo, _ = frame.enu_to_geodetic(all_xy_cat[:, 0], all_xy_cat[:, 1], np.zeros(len(all_xy_cat)))
    _, _, ground_cat = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    ground_cat = np.asarray(ground_cat, np.float64).reshape(-1)
    inside_cat = _in_outlines(outlines, all_xy_cat) if outlines else None
    out = []
    off = 0
    for fid, pi, hint, xy, tang in part_recs:
        n = len(xy)
        ref = ground_cat[off:off + n]
        inside = inside_cat[off:off + n] if inside_cat is not None else None
        off += n
        near = np.zeros(n, bool)
        across = np.zeros(n, bool)
        left_raw = np.column_stack((-tang[:, 1], tang[:, 0]))
        base = xy + left_raw * (NOISE_THICK_M / 2.0)
        toward = left_raw
        pz = np.zeros(n)
        if fits:
            on_bridge = np.full(n, hint is True, bool)
            untagged = np.full(n, hint is None, bool)
            ref, near, pv, pt, ph, pz, pfi, across = _fit_to_carriageways(
                xy, tang, on_bridge, untagged, ref, fits, deck_fits, inside)
            left = np.column_stack((-pt[:, 1], pt[:, 0]))
            p_side = np.where((pv * left).sum(1) < 0.0, 1.0, -1.0)
            _bridge_fit_gaps(xy, ref, fits, inside, near, pv, pt, ph, pz, pfi, p_side, across)
            if near.any():
                left = np.column_stack((-pt[:, 1], pt[:, 0]))
                p_side = np.where((pv * left).sum(1) < 0.0, 1.0, -1.0)
                base = np.where(near[:, None], xy + pv + p_side[:, None] * left * (ph[:, None] + WALL_EDGE_M), base)
                toward = np.where(near[:, None], -p_side[:, None] * left, toward)
        bla, blo, _ = frame.enu_to_geodetic(base[:, 0], base[:, 1], np.zeros(n))
        _, _, z = frame.geodetic_to_enu(bla, blo, np.asarray(elev(bla, blo), np.float64))
        z = np.asarray(z, np.float64)
        on = np.zeros(n, bool)
        if decks:
            surf, ground_s, wide = _surface_info(frame, xy, elev, decks, [hint] * n)
            on = np.abs(surf - ground_s) > 1e-6
            if outlines and hint is True:
                edge = ~on & np.isfinite(wide)
                if edge.any():
                    edge[edge] = inside[edge]
                surf, on = np.where(edge, wide, surf), on | edge
            z = np.where(on, surf, z)
        if only_bridge is True and not on.any():
            continue
        if near.any():
            z = np.where(near, pz, z)
        if decks and hint is None:
            _apply_deck_continuity([np.arange(n)], xy, tang, z, wide, ground_s, decks, roads)
        valid = ~across
        if roads:
            back = base - toward * NOISE_THICK_M
            valid &= ~(_on_road(base, z, roads) | _on_road(back, z, roads))
        if meshed_out is not None:
            meshed_out.add(fid)
        out.extend(_wall_runs_from_mask(base, toward, z + WALL_H, z - NOISE_FOOT_M, valid, box))
    return out


def stone_walls(frame, infra_fn, box, elev, roads=(), decks=()):
    """The mapped stone walls and continuous noise barriers in box (x0, y0, x1, y1) as retaining_walls'
    wall dicts (base, top, foot, toward, thick, back_top, parapet), for roadmesh.tunnel: each mapped line's
    pieces whose segments' middles are in the box (a wall split across tiles meets at the edge, not drawn twice),
    cut where a ground road (roads: build_tile's roads_out, (fid, spec, pts, ...)) runs."""
    if infra_fn is None or (not STONE_ON and not NOISE_ON):
        return []
    x0, y0, x1, y1 = map(float, box)
    keys = _infra_cells(frame, box)
    if not keys:
        return []
    runs, seen = [], set()
    for key in (keys if STONE_ON else ()):
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("stone walls: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            h = stone_height(feats, i)
            if h is None:
                continue
            seen.add(fid)
            if _deck_level_hint(feats, i):
                continue
            try:
                lv = float(np.asarray(feats.attrs.get("level"))[i])
            except (TypeError, ValueError, IndexError):
                lv = 0.0
            if lv > 0:
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                xy = _resample(np.column_stack((e, n)), STONE_STEP_M)
                if len(xy) < 2:
                    continue
                mid = (xy[:-1] + xy[1:]) / 2
                keep = (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
                if not keep.any():
                    continue
                if roads:
                    blocked = _near_road(xy, roads)
                    keep &= ~(blocked[:-1] | blocked[1:])
                j = 0
                while j < len(keep):            # each run of kept segments is a piece
                    if not keep[j]:
                        j += 1
                        continue
                    k = j
                    while k < len(keep) and keep[k]:
                        k += 1
                    runs.append((xy[j:k + 1], h))
                    j = k
    out = []
    if runs:
        allxy = np.concatenate([r for r, _h in runs])
        lat, lon, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], np.zeros(len(allxy)))
        _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
        z = np.asarray(z, np.float64)
        k = 0
        for xy, h in runs:
            zz = z[k:k + len(xy)]
            k += len(xy)
            d = np.gradient(xy, axis=0)
            d /= np.maximum(dnp.hypot(*d.T), 1e-9)[:, None]
            nl = np.column_stack((-d[:, 1], d[:, 0]))
            top = zz + h
            out.append({"base": xy + nl * (STONE_THICK_M / 2), "toward": nl, "top": top, "back_top": top.copy(),
                        "foot": zz - STONE_FOOT_M, "thick": np.full(len(xy), STONE_THICK_M), "parapet": True,
                        "concrete": False, "mountain": False, "rock": False})
    if NOISE_ON:
        meshed = set()
        if decks:
            nw = noise_walls(frame, infra_fn, box, elev, decks=decks, roads=roads, meshed_out=meshed)
            _record_meshed_noise(frame, box, meshed, replace=True)
            out.extend(nw)
        else:
            has_bridge = []
            nw = noise_walls(frame, infra_fn, box, elev, decks=(), roads=roads,
                             only_bridge=False, meshed_out=meshed, bridge_seen_out=has_bridge)
            _record_meshed_noise(frame, box, meshed, replace=True)
            if has_bridge:
                _stash_pending_bridge_noise(box, (frame, infra_fn, tuple(map(float, box)), elev, tuple(roads)))
            out.extend(nw)
    return out


def tile_rows(frame, tx, ty, md, elev):
    """rows() for one road tile."""
    import roads
    box = roads.tile_bbox(tx, ty)

    def infra_fn(key):
        return md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")

    return rows(frame, infra_fn, box, elev)
