"""Tunnel entrances: the glue round earthfast's portal kernels (native/earthfast/src/portal.rs; 2026-11-12, the user:
"focus on tunnel entrances ... Many entrances are broken or ugly and look nothing like the real version").

fit(): per tunnel chunk of a road tile (roads._tunnel_chunks) the ground the entrance is built from: over the tube's
rows ("ground": where it does not cover the tube, roadmesh.tunnel builds the tube's outside), and at each end that is
a portal ("fit"): the ground along the mouth's plane (the headwall's top), beside the approach (the wing walls), the
approach's own heights, how far the headwall may reach either side before another road, and the mouth's look
(earthfast.portal_style: a mountain bore, a cut-and-cover mouth, a lesser road's shorter hood).
structure(): the mesh pieces of one mouth for roadmesh.tunnel.

The ground is the tile's own (the graded ground the terrain shows); nothing here changes a tunnel's floor, sections
or holes. BNE_PORTALS=0: the old portal face (one frame round the opening).
"""
import os

import numpy as np

import fast
import dnp

ON = os.environ.get("BNE_PORTALS", "1") != "0"
# BNE_TUNNEL_ROAD_OVER (aligned with roads.ROAD_OVER_ON, on by default): a road crossing over a mouth keeps its
# surface: the headwall's top under each column a crossing road covers no higher than that road (fit's "cap",
# capped()), not a parapet standing across its lanes (the user's report 72, 2026-11-17: Atlanta Motor Speedway's
# infield tunnel, the service road passing over its south mouth half blocked by the headwall and its soil top).
ROAD_OVER_ON = os.environ.get("BNE_TUNNEL_ROAD_OVER", "on") != "off"
WALL_IN = (0.3,)            # (ROAD_OVER_ON) m inside the mouth's plane the headwall's own footprint is read for roads
LINTEL_MIN = 0.3            # m of headwall over the crown at least under a crossing road (a road nearer the crown than
                            # this: the lintel stands this over it, capped()'s short; the tube's own height is the
                            # tunnel solve's, roads.TunnelProfiles' bore fit)
VERSION = 259               # earthfast's portal kernels
FACE_IN = (1.0, 3.0, 5.0)   # m inside the mouth's plane the ground behind the headwall is read (the highest of them:
                            # the grading flattens the approach a few metres into the mapped mouth, the hill's face
                            # stands 3 - 5 m in; the headwall holds that face, 2026-11-12)
FACE_SIDE = 3.0             # m of headwall beside the opening (over the terrain hole's ragged edge) ...
FACE_SIDE_MIN = 0.7         # ... at least, where another road runs beside the mouth
FACE_STEP = 0.5             # m between the tries of a shorter side
FACE_X = (0.5, 1.5, 3.0)    # m beyond the tube's wall where the ground along the mouth's plane is read ...
ROAD_GAP = 0.4              # m the headwall and the wings keep from another road's carriageway
WING_S = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0)   # m out of the mouth: a wing wall's stations
WING_BACK = 1.2             # m behind a wing wall's face its ground is read
RISE_IN = 12.0              # m inside the mouth where the hill over the tube is read (portal_style's rise)
SIDES_OUT = (5.0, 10.0)     # m out of the mouth where the approach's sides are read (portal_style's sides) ...
SIDES_OFF = 3.0             # ... this far beside the tube's wall
COVER_NEED = 1.0            # m of ground over the crown that covers a tube (less: its outside is built; the terrain's
                            # holes reach TUNNEL_HOLE_MARGIN over the vault, and a square's rise)
COVER_OVER = 0.3            # m the cover's top lies over the crown
COVER_SIDE = 1.5            # m the cover's sides stand outside the tube's wall (past the terrain hole's edge)
COVER_FOOT = 1.0            # m its sides reach under the floor
COVER_REACH = 1.0           # the ground over the tube: the lowest of its axis and this far beyond its walls either side
                            # (the terrain's holes reach TUNNEL_HOLE_MARGIN and a square past the wall)
TWIN_GAP = 0.5              # m a headwall keeps from another tube's wall (a twin bore's collar)
MAJOR = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}

# per look: collar_out, rake, collar_t, wall_t, parapet, top_min, top_max, foot, end_min, wing_t, wing_flare, wing_low
# 0 a mountain bore on a major road: a raked concrete hood (the E39's and the A2's bores, Mapillary 2026-11-12)
# 1 a cut-and-cover mouth: a square-set collar, a lintel with a parapet over the ground, walls straight along the road
# 2 a lesser road's portal in a hillside: a shorter raked concrete hood in a low wall (Rv83 Kilbotn, GitHub #1066: the
#   entrance survey of 2026-11-12 found raked concrete hoods at 5 of its 8 mountain mouths whatever the road, Loibl,
#   Laerdal, Kilbotn, Lantos, Damsgard; masonry only at old ones, Malibu Canyon, the N85, Central Park's arches)
# 3 a low arch (an underpass, a park's transverse): a ring a hand deep in a wall with a parapet, its foot at the floor
# (the hoods: the structure one sees is the hood standing 2 - 4 m out of the slope, the headwall behind it a low ring;
# the first cut, a 0.9 m hood in a headwall 1.8 m over the crown and 3 m beside the opening, read as a flat slab next
# to the Kilbotn / Laerdal photos, 2026-11-12)
LOOKS = {
    0: (2.0, 2.5, 0.55, 0.6, 0.3, 0.6, 1.2, 1.0, 0.8, 0.5, 0.35, 0.5),
    1: (0.25, 0.0, 0.5, 0.6, 1.0, 0.9, 2.5, 1.0, 1.0, 0.5, 0.0, 0.6),
    2: (1.5, 2.0, 0.45, 0.6, 0.3, 0.5, 1.0, 1.0, 0.7, 0.5, 0.3, 0.5),
    3: (0.15, 0.0, 0.4, 0.5, 0.9, 0.6, 2.5, 0.3, 0.9, 0.45, 0.0, 0.6),
}
HOOD_FACE = 0.6             # the hill's face: the ground over the floor this share of the opening's height
LOOK_SIDE = {0: 2.2, 1: FACE_SIDE, 2: 2.0, 3: FACE_SIDE}    # m of headwall beside the opening at most, per look


def _others(roads, cells, frame, parts, tunnels=None, box=None):
    """fast.Others of the roads a mouth's walls keep off: (idx, half_w, xy, road_id, is_tunnel_part, z_arr, is_br)."""
    import mapgeom
    lines = []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        rs = roads.road_specs(feats)
        bs = roads.bridge_specs(feats) if hasattr(roads, "bridge_specs") else rs
        ids = getattr(feats, "ids", None)
        for i, sp in enumerate(rs):
            if sp is not None and sp.kind in roads.BUILT:
                for ring in roads.ground_parts(feats, i):
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) >= 2:
                        lines.append((len(lines), sp.width / 2, xy, None, False, None, False))
            bsp = bs[i] if i < len(bs) else None
            if bsp is not None and bsp.kind in roads.BUILT:
                fid_r = str(ids[i]) if ids is not None and i < len(ids) else None
                rid_r = roads._tunnel_road(fid_r) if fid_r is not None else None
                for ring in roads.bridge_parts(feats, i):
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) >= 2:
                        lines.append((len(lines), bsp.width / 2, xy, rid_r, False, None, True))
    if tunnels is not None and box is not None and getattr(tunnels, "open_parts", None) is not None:
        for fid, spec, xy, z_op, _lon in tunnels.open_parts(cells, box):
            xy_arr = np.asarray(xy, np.float64)[:, :2]
            z_arr = np.asarray(z_op, np.float64).reshape(-1)
            if len(xy_arr) >= 2 and len(z_arr) == len(xy_arr) and np.isfinite(z_arr).all():
                lines.append((len(lines), spec.width / 2, xy_arr, roads._tunnel_road(fid), False, z_arr, False))
    import roadmesh
    for fid, spec, xy, z_p, _lon in parts:
        xy = np.asarray(xy, np.float64)[:, :2]
        z_arr = np.asarray(z_p, np.float64).reshape(-1)
        if len(xy) >= 2:
            # (a twin bore's tube and its collar, not only its carriageway: the two headwalls stopped on each other's
            # roads and overlapped by the shoulders between)
            lines.append((len(lines), spec.width / 2 + roadmesh.TUNNEL_SHOULDER + TWIN_GAP, xy,
                          roads._tunnel_road(fid), True, z_arr if len(z_arr) == len(xy) else None, False))
    return lines


def _crossing(xy, p, t):
    """Does the line xy cross the tube's axis t (unit) at more than 45 degrees where it passes nearest p?"""
    a, b = xy[:-1], xy[1:]
    d = b - a
    tt = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(dnp.hypot(*(a + tt[:, None] * d - p).T)))
    u = d[j] / max(float(dnp.hypot(*d[j])), 1e-9)
    return abs(float(u[0] * t[0] + u[1] * t[1])) < 0.7071


def _through(lines, p, reach, own_roads=None):
    """The indices of the lines passing within reach of p: the mouth's own carriageways (not separate parallel or
    staggered tunnel bores / open approaches / bridges when own_roads is given): not kept off."""
    own = set()
    for row in lines:
        k, _hw, xy = row[0], row[1], row[2]
        if own_roads is not None and len(row) >= 4 and row[3] is not None and row[3] not in own_roads:
            continue
        a, b = xy[:-1], xy[1:]
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        if float(dnp.hypot(*(a + t[:, None] * d - p).T).min()) < reach:
            own.add(k)
    return own


def _line_z_near(xy, z_arr, p, max_d=30.0):
    """(z_min, z_max) of vertices on polyline xy within max_d of p, or None if none / no z_arr."""
    if z_arr is None or len(z_arr) != len(xy):
        return None
    d = dnp.hypot(*(xy - p).T)
    hit = d <= max_d
    if not hit.any():
        return None
    return float(z_arr[hit].min()), float(z_arr[hit].max())


def _same_level_line(row, p, zf, th):
    """True when line `row` sits near mouth elevation (not a high overhead crossing or distant bridge span)."""
    is_br = len(row) >= 7 and bool(row[6])
    if is_br:
        xy = row[2]
        d_end = min(float(dnp.hypot(*(xy[0] - p))), float(dnp.hypot(*(xy[-1] - p))))
        if d_end > 30.0:
            return False
    z_arr = row[5] if len(row) >= 6 else None
    zr = _line_z_near(row[2], z_arr, p)
    if zr is None:
        return True
    return zr[0] <= zf + th - 0.3 and zr[1] >= zf - th


def _hood_clear(oth_surf, oth_tun, p, ul, out, hl, hr, reach_s=4.0, oth_self=None):
    """True if projecting the portal hood forward along out does not collide with another lane or converging bore."""
    xs = (-hr, -0.5 * hr, 0.0, 0.5 * hl, hl)
    for s_fwd in (1.0, 2.0, min(3.5, reach_s)):
        base = p + out * s_fwd
        for x in xs:
            q = base + ul * x
            if not oth_surf.clear(q, None, ROAD_GAP) or not oth_tun.clear(q, None, 0.25):
                return False
            if oth_self is not None and not oth_self.clear(q, None, 0.25):
                return False
    return True


def _bore_self_lines(xy, s_bore, at_end, hw_keep, min_arc):
    """Segments of bore `xy` separated from mouth `at_end` by >= min_arc along the bore (U-turn / S-curve keep-off)."""
    if len(xy) < 3 or len(s_bore) != len(xy):
        return []
    tot = float(s_bore[-1])
    if tot < min_arc + 1.0:
        return []
    segs = []
    for j in range(len(xy) - 1):
        d_arc = float(s_bore[j]) if at_end == 0 else (tot - float(s_bore[j + 1]))
        if d_arc >= min_arc:
            segs.append((len(segs), hw_keep, np.asarray(xy[j:j + 2], np.float64)))
    return segs


def _curve_dev_max(xy, s_bore, end_idx, p_e, out_e, max_s):
    """Max XY offset between linear portal tangent `p_e - out_e * s` and curved bore axis within `max_s` of mouth."""
    tot = float(s_bore[-1]) if len(s_bore) else 0.0
    lim = min(tot, float(max_s))
    if lim <= 1e-6 or len(xy) < 3:
        return 0.0
    sq = np.linspace(0.0, lim, 7)
    s_pos = sq if end_idx == 0 else (tot - sq)
    px = dnp.interp(s_pos, s_bore, xy[:, 0])
    py = dnp.interp(s_pos, s_bore, xy[:, 1])
    lx = float(p_e[0]) - float(out_e[0]) * sq
    ly = float(p_e[1]) - float(out_e[1]) * sq
    return float(dnp.hypot(px - lx, py - ly).max())


def _trim_curved_bore_tris(pm, tm, uvm, cm, bxy, bz, clr_hw, th):
    """Drop exterior portal/cover triangles that slice into a curved bore's interior clearance prism."""
    pm_arr = np.asarray(pm, np.float64)
    tm_arr = np.asarray(tm, np.int64)
    uvm_arr = np.asarray(uvm, np.float64)
    cm_arr = np.asarray(cm, np.int64)
    if not len(pm_arr) or not len(tm_arr) or len(bxy) < 2:
        return pm_arr, tm_arr, uvm_arr, cm_arr
    seg = np.diff(bxy, axis=0)
    slen = np.maximum(dnp.hypot(seg[:, 0], seg[:, 1]), 1e-9)
    slen2 = slen * slen
    s_nodes = np.r_[0.0, np.cumsum(slen)]
    tot_l = float(s_nodes[-1])
    if tot_l <= 0.9:
        return pm_arr, tm_arr, uvm_arr, cm_arr
    wall_h = 0.676 * float(th)
    z_ceil = max(2.2, min(wall_h - 0.12, float(th) - 0.85))
    tv = pm_arr[tm_arr]
    keep = np.ones(len(tm_arr), dtype=bool)
    for ti in range(len(tm_arr)):
        tri = tv[ti]
        probes = (
            (tri[0] + tri[1] + tri[2]) / 3.0,
            tri[0],
            tri[1],
            tri[2],
            0.5 * (tri[0] + tri[1]),
            0.5 * (tri[1] + tri[2]),
            0.5 * (tri[2] + tri[0]),
        )
        for pr in probes:
            rel = pr[:2] - bxy[:-1]
            t = np.clip((rel[:, 0] * seg[:, 0] + rel[:, 1] * seg[:, 1]) / slen2, 0.0, 1.0)
            proj = bxy[:-1] + t[:, None] * seg
            dd = dnp.hypot(pr[0] - proj[:, 0], pr[1] - proj[:, 1])
            kk = int(np.argmin(dd))
            best_d = float(dd[kk])
            if best_d >= clr_hw:
                continue
            best_s = float(s_nodes[kk]) + float(t[kk]) * float(slen[kk])
            if best_s < 0.40 or best_s > tot_l - 0.40:
                continue
            best_zf = float(bz[kk]) + float(t[kk]) * float(bz[kk + 1] - bz[kk])
            h_rel = float(pr[2]) - best_zf
            if 0.25 <= h_rel <= z_ceil:
                keep[ti] = False
                break
    if not keep.all():
        tm_arr = tm_arr[keep]
        cm_arr = cm_arr[keep]
    return pm_arr, tm_arr, uvm_arr, cm_arr


def _cover_ground(xy, z, left, hw, th, g_rows, oth_surf, oth_tun=None, wlr=None):
    """Ground over tube's rows, suppressing cover emission on bare rows colliding with a surface lane or twin bore."""
    k = len(xy)
    row_g = np.minimum(np.minimum(g_rows[:k], g_rows[k:2 * k]), g_rows[2 * k:3 * k])
    if oth_surf is None and oth_tun is None and wlr is None:
        return [round(float(v), 2) for v in row_g]
    out_g = []
    for ri in range(k):
        val = float(row_g[ri])
        if val - float(z[ri]) < th + COVER_NEED:
            suppress = False
            if wlr is not None and ri < len(wlr):
                if float(wlr[ri][0]) < hw - 0.05 or float(wlr[ri][1]) < hw - 0.05:
                    suppress = True
            if not suppress:
                for sg in (1.0, -1.0):
                    for off in (hw, hw + COVER_SIDE - 0.1):
                        q_lat = xy[ri] + left[ri] * (sg * off)
                        if oth_surf is not None and not oth_surf.clear(q_lat, None, ROAD_GAP):
                            suppress = True
                            break
                        if oth_tun is not None and not oth_tun.clear(q_lat, None, 0.0):
                            suppress = True
                            break
                    if suppress:
                        break
            if suppress:
                val = float(z[ri]) + th + COVER_NEED + 1.0
        out_g.append(round(val, 2))
    return out_g


def fit(roads, tunnels, cells, chunks, parts):
    """chunks with "ground" and "fit" on the tunnel tubes' (the module's head). roads: the roads module."""
    import roadmesh
    if not ON or getattr(tunnels, "_ground", None) is None:
        return chunks
    todo = [c for c in chunks if c.get("tunnel") and not c.get("no_tube") and len(c["n"]) >= 2]
    if not todo:
        return chunks
    fast.require(VERSION, "portal_style")
    xy_all = np.vstack([np.asarray(c["n"], np.float64)[:, :2] for c in todo])
    fit_box = (float(xy_all[:, 0].min()) - 80.0, float(xy_all[:, 1].min()) - 80.0,
               float(xy_all[:, 0].max()) + 80.0, float(xy_all[:, 1].max()) + 80.0)
    others = None
    for c in todo:
        n = np.asarray(c["n"], np.float64)
        xy, z = n[:, :2], n[:, 2]
        hw = float(c["w"]) / 2 + roadmesh.TUNNEL_SHOULDER
        th = float(c.get("tunnel_h") or roadmesh.TUNNEL_H)
        seg_d = dnp.hypot(*np.diff(xy, axis=0).T)
        s_bore = np.r_[0.0, np.cumsum(seg_d)]
        chunk_len = float(s_bore[-1])
        portals_c = c.get("portal") or (False, False)
        n_portals = sum(1 for pf in portals_c if pf)
        sb_lim = max(0.0, (0.4 * chunk_len - 1.0) if n_portals >= 2 else (chunk_len - 1.5))
        if n_portals >= 2 and chunk_len < 15.0:
            sb_lim = min(sb_lim, max(0.0, 0.25 * chunk_len - 2.0))
        d = np.gradient(xy, axis=0)
        d /= np.maximum(dnp.hypot(*d.T), 1e-9)[:, None]
        left = np.c_[-d[:, 1], d[:, 0]]
        pts = [xy, xy + left * (hw + COVER_REACH), xy - left * (hw + COVER_REACH)]
        if others is None:
            others = _others(roads, cells, tunnels.frame, parts, tunnels=tunnels, box=fit_box)
        fid_c = c.get("fid")
        own_rid = roads._tunnel_road(fid_c) if fid_c is not None else None
        own_roads = ({own_rid} | set(c.get("twin_roads") or ())) if own_rid is not None else None
        own_all = _through(others, xy[0], hw, own_roads=own_roads) | _through(others, xy[-1], hw, own_roads=own_roads)
        own_only = (
            _through(others, xy[0], hw, own_roads={own_rid})
            | _through(others, xy[-1], hw, own_roads={own_rid})
        ) if own_rid is not None else own_all
        zf_mid = float(np.median(z))
        cov_surf = [ln[:3] for ln in others if ln[0] not in own_all and not (len(ln) >= 5 and ln[4])
                    and _same_level_line(ln, xy[len(xy) // 2], zf_mid, th)]
        cov_tun = [ln[:3] for ln in others if ln[0] not in own_only and (len(ln) >= 5 and ln[4])
                   and _same_level_line(ln, xy[len(xy) // 2], zf_mid, th)]
        oth_cov = fast.Others(cov_surf) if cov_surf else None
        oth_cov_tun = fast.Others(cov_tun) if cov_tun else None
        ends = []
        wlr = c.get("wall_lr")
        for e, at in enumerate((0, len(xy) - 1)):
            if not portals_c[e]:
                ends.append(None)
                continue
            t = xy[1] - xy[0] if e == 0 else xy[-1] - xy[-2]
            t = t / max(float(dnp.hypot(*t)), 1e-9)
            ul = np.array([-t[1], t[0]])
            out = -t if e == 0 else t
            p = xy[at]
            zf_e = float(z[at])
            own = _through(others, p, hw, own_roads=own_roads)
            non_own = [ln for ln in others if ln[0] not in own]
            lvl_non_own = [ln for ln in non_own if _same_level_line(ln, p, zf_e, th)]
            oth = fast.Others([ln[:3] for ln in lvl_non_own])
            oth_tun = fast.Others([ln[:3] for ln in lvl_non_own if len(ln) >= 5 and ln[4]])
            oth_surf = fast.Others([ln[:3] for ln in lvl_non_own if not (len(ln) >= 5 and ln[4])])
            self_segs = _bore_self_lines(xy, s_bore, at, hw + TWIN_GAP, max(12.0, hw + FACE_SIDE + 3.0))
            oth_self = fast.Others(self_segs) if self_segs else None
            sides = []
            wing_ok = []
            ws = np.asarray(WING_S, np.float64)
            ws_check = (-2.0, 0.0) + tuple(WING_S)
            for sg in (1.0, -1.0):          # how far the headwall reaches before another road or parallel bore
                reach = FACE_SIDE
                blocked_by_tun = False
                for dd in np.arange(0.0, FACE_SIDE + 1e-9, FACE_STEP):
                    q_probe = p + ul * sg * (hw + dd)
                    self_hit = oth_self is not None and (
                        not oth_self.clear(q_probe, None, ROAD_GAP)
                        or not oth_self.clear(q_probe + out * 1.0, None, ROAD_GAP)
                    )
                    if self_hit or not oth.clear(q_probe, None, ROAD_GAP) or not oth.clear(q_probe + out * 1.0, None, ROAD_GAP):
                        blocked_by_tun = (
                            self_hit
                            or not oth_tun.clear(q_probe, None, ROAD_GAP)
                            or not oth_tun.clear(q_probe + out * 1.0, None, ROAD_GAP)
                        )
                        reach = max(float(dd) - FACE_STEP, 0.0 if (blocked_by_tun or dd < FACE_SIDE_MIN) else FACE_SIDE_MIN)
                        break
                sides.append(reach)
                ok = (reach >= FACE_SIDE - 1e-9) and not blocked_by_tun
                if ok:
                    lp = [
                        p + ul * sg * lat_off + out * s
                        for s in ws_check
                        for lat_off in (hw + 0.5, hw + reach + 0.5)
                    ]
                    ok = all(oth.clear(q, None, ROAD_GAP) for q in lp)
                    if ok and oth_self is not None:
                        ok = all(oth_self.clear(q, None, ROAD_GAP) for q in lp)
                if ok and not oth_tun.clear(p + ul * sg * (hw + FACE_SIDE + 2.5), None, 1.0):
                    ok = False
                if ok and oth_self is not None and not oth_self.clear(p + ul * sg * (hw + FACE_SIDE + 2.5), None, 1.0):
                    ok = False
                wing_ok.append(ok)
            hl_e = float(wlr[at][0]) if wlr is not None and at < len(wlr) else hw
            hr_e = float(wlr[at][1]) if wlr is not None and at < len(wlr) else hw
            hood_ok = _hood_clear(oth_surf, oth_tun, p, ul, out, hl_e, hr_e, oth_self=oth_self)
            if hood_ok and _curve_dev_max(xy, s_bore, e, p, out, 5.0) > 0.45:
                hood_ok = False
            mouth_overlapped = any(
                not oth_surf.clear(p + ul * sg * max(0.25, hw - 0.1), None, 0.0) for sg in (1.0, -1.0)
            )
            fx = np.array([-(hw + v) for v in FACE_X[::-1]] + [-0.5 * hw, 0.0, 0.5 * hw] + [hw + v for v in FACE_X])
            look = None
            # (ROAD_OVER_ON: which of the headwall's ground samples lie on another road's carriageway, a road crossing
            # over the mouth among them: one through it at more than 45 degrees to the tube, not its own approach)
            over = None
            over_min_x = None
            if ROAD_OVER_ON:
                cross_rows = []
                cross_min = []
                for ln in others:
                    if len(ln) >= 5 and ln[4]:
                        continue
                    is_cr = _crossing(ln[2], p, t)
                    if ln[0] not in own or is_cr:
                        cross_rows.append(ln[:3])
                        cross_min.append(max(1.5, 0.55 * th) if is_cr else (th - 0.3))
                cross = fast.Others(cross_rows)
                xl_max = hw + max(sides[0], 0.65)
                xr_min = -(hw + max(sides[1], 0.65))
                in_wall = (fx >= xr_min - 1e-6) & (fx <= xl_max + 1e-6)
                over = np.array([[bool(ok_x) and not cross.clear(p + ul * x - out * s_in, None, 0.0)
                                  for x, ok_x in zip(fx, in_wall)]
                                 for s_in in WALL_IN + FACE_IN], bool)
                # Store per-column floor threshold: 0.55*th when any crossing line hits column, else th-0.3
                over_cross = fast.Others([r for r, m in zip(cross_rows, cross_min) if m < th - 0.35])
                over_min_x = [max(1.5, 0.55 * th) if any(not over_cross.clear(p + ul * x - out * s_in, None, 0.0)
                                                         for s_in in WALL_IN + FACE_IN) else (th - 0.3)
                              for x in fx]
            ends.append({"e": e, "p": p, "ul": ul, "out": out, "fx": fx, "sides": sides, "wing_ok": wing_ok, "at": at,
                         "i0": sum(len(q) for q in pts), "look": look, "over": over, "over_min_x": over_min_x,
                         "oth": oth, "oth_self": oth_self, "sb_lim": sb_lim, "hood_ok": hood_ok,
                         "mouth_overlapped": mouth_overlapped})
            for s_in in FACE_IN:
                pts.append(p[None] + ul[None] * fx[:, None] - out[None] * s_in)
            for sg, reach in zip((1.0, -1.0), sides):
                pts.append(p[None] + ul[None] * sg * (hw + reach + WING_BACK) + out[None] * ws[:, None])
            pts.append(p[None] + out[None] * ws[:, None])
            pts.append((p - out * RISE_IN)[None])
            pts.append(np.array([p + ul * sg * (hw + SIDES_OFF) + out * s for sg in (1.0, -1.0) for s in SIDES_OUT]))
            if over is not None:    # (the wall's footprint rows, last: the default build's samples as they were)
                ends[-1]["iw"] = sum(len(q) for q in pts)
                for s_in in WALL_IN:
                    pts.append(p[None] + ul[None] * fx[:, None] - out[None] * s_in)
        try:
            g = np.asarray(tunnels._ground(np.vstack(pts)), np.float64)
        except Exception:               # (no ground at hand: the plain structure, the tube as it was)
            continue
        k = len(xy)
        c["ground"] = _cover_ground(xy, z, left, hw, th, g, oth_cov, oth_tun=oth_cov_tun, wlr=wlr)
        fits = []
        for en in ends:
            if en is None:
                fits.append(None)
                continue
            i, nx, nw = en["i0"], len(en["fx"]), len(WING_S)
            zf = float(z[en["at"]])
            e_idx = int(en["e"])

            def _z_ref(s_in_m):
                s_pos = min(chunk_len, float(s_in_m)) if e_idx == 0 else max(0.0, chunk_len - float(s_in_m))
                return max(zf, float(dnp.interp(s_pos, s_bore, z)))

            rows_rel = [g[i + q * nx:i + (q + 1) * nx] - _z_ref(s_in) for q, s_in in enumerate(FACE_IN)]
            face = np.max(rows_rel, axis=0)
            p_e, ul_e, out_e, oth_e = en["p"], en["ul"], en["out"], en["oth"]
            if (_z_ref(7.0) - zf) > 0.35 or _curve_dev_max(xy, s_bore, e_idx, p_e, out_e, 7.0) > 0.35:
                face = np.minimum(face, th + 2.8)
            # (ROAD_OVER_ON: per column the lowest of a crossing road's samples over the floor: the wall's top there no
            # higher, whatever higher bank stands behind it; Codex review of 80e9b3da)
            cap = None
            if en["over"] is not None and en["over"].any():
                iw = en["iw"]
                wall_rel = [g[iw + q * nx:iw + (q + 1) * nx] - _z_ref(s_in) for q, s_in in enumerate(WALL_IN)]
                rd = np.minimum(np.where(en["over"], np.asarray(wall_rel + rows_rel), np.inf).min(axis=0), face)
                mins_x = en["over_min_x"] or [th - 0.3] * len(rd)
                cap_vals = [round(float(v), 2) if (np.isfinite(v) and float(v) >= m_x) else None
                            for v, m_x in zip(rd, mins_x)]
                if any(v is not None for v in cap_vals):
                    cap = cap_vals
            # the hill's face over the axis: how far in the graded approach goes on before it (a hood reaches out of
            # the face to the mouth: Laerdal's stands ~5 m out of its slope)
            setback = 0.0
            hw_core = max(hw - 0.3, 0.5)
            for q, s_in in enumerate(FACE_IN):
                if s_in > en["sb_lim"] + 1e-9:
                    break
                if _curve_dev_max(xy, s_bore, e_idx, p_e, out_e, s_in + 6.5) > 0.25:
                    break
                p_in = p_e - out_e * s_in
                if not (oth_e.clear(p_in, None, 0.0)
                        and oth_e.clear(p_in + ul_e * hw_core, None, 0.0)
                        and oth_e.clear(p_in - ul_e * hw_core, None, 0.0)):
                    break
                if float(rows_rel[q][nx // 2]) >= HOOD_FACE * th:
                    break
                setback = s_in
            i += len(FACE_IN) * nx
            wl, wr, rz = g[i:i + nw] - zf, g[i + nw:i + 2 * nw] - zf, g[i + 2 * nw:i + 3 * nw] - zf
            i += 3 * nw
            sd = g[i + 1:i + 5] - zf
            # (the approach's sides over the road out there: the lower side's, the deeper of its two stations)
            rz_s = dnp.interp(SIDES_OUT, WING_S, rz)
            sides_h = min(float((sd[:2] - rz_s).max()), float((sd[2:] - rz_s).max()))
            # how far the hill over the tube climbs over the approach's sides (a bore into a slope: metres; a box under
            # a street level with its trough's walls: none). Over the mouth's own face ground it read nothing once
            # the face is read 5 m in, at the hill's face (Kilbotn, Laerdal: flat lintels, 2026-11-12)
            rise = float(g[i] - _z_ref(RISE_IN)) - max(sides_h, 0.0)
            st = int(fast._ef.portal_style(c.get("cls") in MAJOR or bool(c.get("concrete")), th, rise, sides_h))
            r2 = lambda v: [round(float(x), 2) for x in v]      # noqa: E731
            fits.append({"st": st, "fx": r2(en["fx"]), "fg": r2(face), "ws": list(WING_S),
                         "wl": r2(wl) if en["wing_ok"][0] else None, "wr": r2(wr) if en["wing_ok"][1] else None,
                         "rz": r2(rz), "sl": round(en["sides"][0], 2), "sr": round(en["sides"][1], 2),
                         "sb": round(setback, 2) if st in (0, 2) else 0.0})
            if not en["hood_ok"]:
                fits[-1]["co"] = 0.0
            if en["mouth_overlapped"]:
                fits[-1]["skip"] = True
            if cap is not None:         # (only then: the default build's fits as they were)
                fits[-1]["cap"] = cap
            self_near = en["oth_self"] is not None and not en["oth_self"].clear(p_e, None, 4.0)
            if _curve_dev_max(xy, s_bore, e_idx, p_e, out_e, 18.0) > 0.15 or self_near:
                xy_ord = xy if e_idx == 0 else xy[::-1]
                z_ord = z if e_idx == 0 else z[::-1]
                lim_n = min(len(xy_ord), 96)
                fits[-1]["bxy"] = [
                    [round(float(pt[0] - p_e[0]), 3), round(float(pt[1] - p_e[1]), 3)]
                    for pt in xy_ord[:lim_n]
                ]
                fits[-1]["bz"] = [round(float(zv - zf), 3) for zv in z_ord[:lim_n]]
                fits[-1]["chw"] = round(float(c["w"]) / 2.0, 3)
        if any(f is not None for f in fits):
            c["fit"] = fits
    return chunks


def _look(fitd, low):
    return 3 if low else int(fitd["st"])


def capped(fitd, th, low=False):
    """(face ground (fx,), top_min, short): fitd's headwall ground and its look's top_min with a crossing road's cap
    (fit's "cap", BNE_TUNNEL_ROAD_OVER) taken in: the ground under each capped column the road's less the parapet, so
    the wall's top there is the road's, and top_min no more than the road's height over the crown at the opening's
    columns, LINTEL_MIN at least (short: a road nearer the crown than that, the lintel standing over it)."""
    lk = LOOKS[_look(fitd, low)]
    fg = np.asarray(fitd["fg"], np.float64)
    tmin = float(lk[5])
    cap = fitd.get("cap")
    if not cap:
        return fg, tmin, False
    cv = np.array([np.inf if v is None else float(v) for v in cap], np.float64)
    # (one column further each way: the mesh kernel's top takes its breakpoints between the samples and drops a sample
    # near one, interpolating past a lone capped column; Codex review of b68ade87)
    cv = np.minimum(cv, np.minimum(np.r_[cv[1:], np.inf], np.r_[np.inf, cv[:-1]]))
    fg = np.minimum(fg, cv - float(lk[4]))
    on = np.isfinite(cv)
    if not on.any():
        return fg, tmin, False
    room = float(cv[on].min()) - th
    return fg, max(LINTEL_MIN, min(tmin, room)), room < LINTEL_MIN


def parapet(fitd, low=False):
    return LOOKS[_look(fitd, low)][4]


def _fit_tmax(fitd, th, lk):
    """Upper lintel headroom over crown for mountain/cut-and-cover portals when steep terrain holes sit higher."""
    tmax = float(lk[6])
    if fitd is None or fitd.get("cap") or int(fitd.get("st", 0)) not in (0, 1, 2):
        return tmax
    fx = fitd.get("fx") or ()
    fg = fitd.get("fg") or ()
    if len(fx) and len(fx) == len(fg):
        fmax = max(float(v) for v in fg)
        if fmax > th + tmax:
            tmax = max(tmax, min(fmax - th + 0.5, 2.8))
    return tmax


def crown_top(fitd, th, low=False):
    """The headwall's top over the floor at the opening's middle (earthfast.portal's headwall_top there)."""
    lk = LOOKS[_look(fitd, low)]
    fg, tmin, _short = capped(fitd, th, low)
    g = float(dnp.interp(0.0, fitd["fx"], fg))
    return min(max(g + lk[4], th + tmin), th + _fit_tmax(fitd, th, lk))


def prm(st, th, hw, side_l, side_r, uv_m, fitd=None):
    """earthfast.portal's Prm for look st."""
    lk = LOOKS[st]
    co, rake, ct, wt, par, tmin, tmax, foot, emin, wgt, flare, wlow = lk
    if fitd is not None and fitd.get("co") is not None:
        co = float(fitd["co"])
        if co <= 0.02:
            rake = 0.0
    tmax = _fit_tmax(fitd, th, lk)
    side_l, side_r = min(side_l, LOOK_SIDE[st]), min(side_r, LOOK_SIDE[st])
    return np.array([th, hw, co, rake, ct, wt, par, side_l, tmin, tmax, foot, emin, wgt, flare, wlow, uv_m, side_r,
                     COVER_SIDE + 0.1], np.float64)


def setback(fitd, low=False):
    """m the mouth's headwall stands back from its mouth (a hood's, at the hill's face)."""
    return 0.0 if low or fitd is None else float(fitd.get("sb") or 0.0)


def structure(fitd, p, ul, out, sec, th, hw, uv_m, walls_near=(False, False), low=False):
    """The mouth's structure for roadmesh.tunnel: (positions (n, 3), triangles (m, 3), uvs (n, 2), codes (m,): 0 the
    structure, 1 soil). p: the mouth's floor centre in the mesh's coordinates; walls_near: (left, right) a cutting's
    retaining wall already stands there (no wing wall on that side)."""
    fast.require(VERSION, "portal_mesh")
    if fitd is not None and fitd.get("skip"):
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 2)), np.zeros(0, np.int64)
    st = _look(fitd, low)
    none = np.zeros(0)
    # a hood: its headwall back at the hill's face, the hood reaching out from there to the mouth
    sb = setback(fitd, low)
    p_mouth = np.asarray(p, np.float64)
    p = p_mouth - np.r_[np.asarray(out, np.float64) * sb, 0.0]
    ul_v = np.asarray(ul, np.float64)
    ws = np.asarray(fitd["ws"], np.float64) + sb
    wl = np.asarray(fitd["wl"], np.float64) if fitd.get("wl") is not None and not walls_near[0] else none
    wr = np.asarray(fitd["wr"], np.float64) if fitd.get("wr") is not None and not walls_near[1] else none
    pr = prm(st, th, hw, float(fitd["sl"]), float(fitd["sr"]), uv_m, fitd=fitd if not low else None)
    fg, pr[8], _short = capped(fitd, th, low)       # (a crossing road's cap: BNE_TUNNEL_ROAD_OVER)
    pr[2] += sb
    pr = np.r_[pr, sb]          # (inner_from: the tube's own walls are the hood's inside up to the mouth)
    sec_arr = np.asarray(sec, np.float64)
    max_l = float(sec_arr[:, 0].max()) if len(sec_arr) >= 5 else float(hw)
    max_r = float(-sec_arr[:, 0].min()) if len(sec_arr) >= 5 else float(hw)
    trim_l = 0.05 < float(hw) - max_l and max_l > 0.25
    trim_r = 0.05 < float(hw) - max_r and max_r > 0.25
    sec_use = sec_arr
    if trim_l or trim_r:
        sec_use = sec_arr.copy()
        if trim_l:
            sec_use[sec_use[:, 0] >= 0.0, 0] *= float(hw) / max_l
        if trim_r:
            sec_use[sec_use[:, 0] < 0.0, 0] *= float(hw) / max_r
    pm, tm, uvm, cm = fast._ef.portal_mesh(
        [float(v) for v in p], [float(v) for v in ul_v], [float(v) for v in out], pr,
        np.ascontiguousarray(sec_use, np.float64), np.asarray(fitd["fx"], np.float64),
        np.ascontiguousarray(fg, np.float64), ws, wl, wr, np.asarray(fitd["rz"], np.float64))
    if (trim_l or trim_r) and len(pm) and len(tm):
        pm_arr = np.asarray(pm, np.float64).copy()
        lat = (pm_arr[:, 0] - float(p[0])) * float(ul_v[0]) + (pm_arr[:, 1] - float(p[1])) * float(ul_v[1])
        if trim_l:
            ov_l = np.maximum(0.0, lat - max_l)
            pm_arr[:, 0] -= ov_l * float(ul_v[0])
            pm_arr[:, 1] -= ov_l * float(ul_v[1])
        if trim_r:
            ov_r = np.minimum(0.0, lat + max_r)
            pm_arr[:, 0] -= ov_r * float(ul_v[0])
            pm_arr[:, 1] -= ov_r * float(ul_v[1])
        pm, tm, uvm, cm = pm_arr, np.asarray(tm, np.int64), np.asarray(uvm, np.float64), np.asarray(cm, np.int64)
    if fitd is not None and fitd.get("bxy") and len(pm) and len(tm):
        bxy = p_mouth[:2][None, :] + np.asarray(fitd["bxy"], np.float64)
        bz = float(p_mouth[2]) + np.asarray(fitd.get("bz") or np.zeros(len(bxy)), np.float64)
        chw = float(fitd.get("chw") or max(0.5, float(hw) - 1.2))
        clr_hw = max(0.45, min(float(hw) - 0.12, chw + 0.15))
        return _trim_curved_bore_tris(pm, tm, uvm, cm, bxy, bz, clr_hw, th)
    return pm, tm, uvm, cm


def cover(mid, left, z, ground, hw, th, caps, uv_m, soil_uv_m, sec=None):
    """The tube's outside over its bare rows for roadmesh.tunnel (earthfast.tunnel_cover_mesh); sec: the tube's section
    (roadmesh._tunnel_section), the opening its end caps inside the hill leave clear."""
    fast.require(VERSION, "tunnel_cover_mesh")
    mid_arr = np.ascontiguousarray(mid, np.float64)
    z_arr = np.ascontiguousarray(z, np.float64)
    pm, tm, uvm, cm = fast._ef.tunnel_cover_mesh(
        mid_arr, np.ascontiguousarray(left, np.float64), z_arr, np.ascontiguousarray(ground, np.float64),
        float(hw + COVER_SIDE), float(th), COVER_OVER, COVER_NEED, COVER_FOOT,
        bool(caps[0]), bool(caps[1]), float(uv_m), float(soil_uv_m),
        np.ascontiguousarray(np.zeros((0, 2)) if sec is None else sec, np.float64))
    if len(mid_arr) >= 3 and len(pm) and len(tm):
        ch = mid_arr[-1] - mid_arr[0]
        clen = float(dnp.hypot(ch[0], ch[1]))
        if clen <= 1e-6:
            curved = True
        else:
            rel = mid_arr - mid_arr[0]
            perp = np.abs(rel[:, 0] * (-ch[1]) + rel[:, 1] * ch[0]) / clen
            curved = float(perp.max()) > 0.15
        if curved:
            clr_hw = max(0.45, float(hw) - 1.05)
            return _trim_curved_bore_tris(pm, tm, uvm, cm, mid_arr, z_arr, clr_hw, th)
    return pm, tm, uvm, cm
