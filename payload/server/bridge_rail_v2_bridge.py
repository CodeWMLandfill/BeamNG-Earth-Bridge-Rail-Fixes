"""Grounded v2host / earthfast 607 runtime solver bridge for BeamNG-Earth-Bridge-Rail-Fixes.

Preserves all upstream dev.567 exports, signatures, and SURFACE_V2 / v2host call contracts
while applying verified Bridge & Rail geometry, clearance, parapet, tunnel-vault, abutment,
and plan-cache invalidation fixes directly onto live module namespaces.
"""
import functools
import math
import numpy as np

BUTT_GAP_M = 3.5
BUTT_COS = -0.82
BUTT_LATERAL = 0.45
BUTT_WELD_EASE_M = 12.0

ABUT_AGREE_M = 0.50
ABUT_LOW_M = 0.20
ABUT_SHOULDER_TAPER_M = 8.0
APPROACH_WALL_SIDE_RUN = 1.35
DECK_GROUND_CLIFF_M = 1.60
PIN_SNAP_M = 0.35

PIER_SHAFT_T = 0.72
LOW_RISE = 0.25


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def abutment_z(z_graded, z_lidar, z_dem):
    """Shared elevation at a bridge abutment meeting a graded ground road."""
    zg, zl, zd = _finite(z_graded), _finite(z_lidar), _finite(z_dem)
    if zg is not None and zd is not None and abs(zd - zg) <= ABUT_AGREE_M:
        return zg
    if zg is not None and zl is not None and abs(zl - zg) <= ABUT_AGREE_M:
        return zg
    if zl is not None and zd is not None and abs(zl - zd) <= ABUT_AGREE_M:
        return zl
    if zg is not None:
        return zg
    if zl is not None:
        return zl
    return zd


def _butt_outward(xy, end):
    if len(xy) < 2:
        return np.array([1.0, 0.0], np.float64)
    v = (xy[0] - xy[1]) if end == 0 else (xy[-1] - xy[-2])
    n = float(np.hypot(v[0], v[1]))
    return (v / n) if n > 1e-9 else np.array([1.0, 0.0], np.float64)


def weld_deck_butts(parts, ground_lines=(), ground_z=None):
    """Meet collinear open-ended elevated bridge decks stopping <= BUTT_GAP_M apart."""
    del ground_lines, ground_z
    if len(parts) < 2:
        return list(parts)
    owned = []
    for fid, spec, xy, z, lon in parts:
        xy_arr = np.array(np.asarray(xy, np.float64)[:, :2], copy=True)
        z_arr = np.array(np.asarray(z, np.float64), copy=True)
        owned.append([fid, spec, xy_arr, z_arr, lon])
    ends = []
    for i, (_fid, spec, xy_arr, z_arr, _lon) in enumerate(owned):
        if len(xy_arr) < 2 or spec is None:
            continue
        for end in (0, -1):
            ends.append((i, end, xy_arr[end], float(z_arr[end])))
    used = set()
    for a_idx in range(len(ends)):
        ia, ea, pa, za = ends[a_idx]
        if (ia, ea) in used:
            continue
        ua = _butt_outward(owned[ia][2], ea)
        w_a = float(getattr(owned[ia][1], "width", 8.0) or 8.0)
        for b_idx in range(a_idx + 1, len(ends)):
            ib, eb, pb, zb = ends[b_idx]
            if ia == ib or (ib, eb) in used:
                continue
            gap_vec = pb - pa
            gap = float(np.hypot(gap_vec[0], gap_vec[1]))
            if gap > BUTT_GAP_M or abs(za - zb) > 1.25:
                continue
            ub = _butt_outward(owned[ib][2], eb)
            if float(ua @ ub) > BUTT_COS:
                continue
            w_b = float(getattr(owned[ib][1], "width", 8.0) or 8.0)
            lat = abs(float(gap_vec[0] * (-ua[1]) + gap_vec[1] * ua[0]))
            if lat > BUTT_LATERAL * max(w_a, w_b):
                continue
            mid_xy = 0.5 * (pa + pb)
            mid_z = 0.5 * (za + zb)
            owned[ia][2][ea] = mid_xy
            owned[ib][2][eb] = mid_xy
            owned[ia][3][ea] = mid_z
            owned[ib][3][eb] = mid_z
            used.add((ia, ea))
            used.add((ib, eb))
            break
    return [tuple(row) for row in owned]


def _other_ground_roads(ns, mapgeom, parts, cells, frame):
    own = {str(p[0]).split("#")[0] for p in parts}
    ends_list = [np.asarray(p[2], np.float64)[k, :2] for p in parts for k in (0, -1) if len(p[2])]
    ends = np.array(ends_list, np.float64) if ends_list else np.empty((0, 2), np.float64)
    others = []
    road_specs = ns.get("road_specs")
    ground_parts = ns.get("ground_parts")
    built = ns.get("BUILT", ())
    hole_join = float(ns.get("HOLE_JOIN_M", 1.0))
    if not callable(road_specs) or not callable(ground_parts):
        return others
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        rs = road_specs(feats)
        for i, sp in enumerate(rs):
            fid = str(feats.ids[i])
            if sp is None or sp.kind not in built or fid in own:
                continue
            for ring in ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.c_[e, n]
                if len(xy) < 2:
                    continue
                if len(ends) and min(
                    float(np.min(np.hypot(*(ends - xy[0]).T))),
                    float(np.min(np.hypot(*(ends - xy[-1]).T))),
                ) < hole_join:
                    continue
                others.append((fid, sp.width / 2.0, xy))
    return others


def apply_roads_hooks(ns):
    """Apply verified tunnel-mouth, crossing-road arch-raise, and butt-weld fixes onto roads."""
    if ns.get("_BRF_V2_ROADS_HOOKED"):
        return
    ns["TUNNEL_LOW_H"] = max(float(ns.get("TUNNEL_LOW_H", 3.6)), 4.0)
    ns["ROAD_CAP_OVER_M"] = min(float(ns.get("ROAD_CAP_OVER_M", 1.0)), 0.4)
    ns["BUTT_GAP_M"] = BUTT_GAP_M
    ns["BUTT_COS"] = BUTT_COS
    ns["BUTT_LATERAL"] = BUTT_LATERAL
    ns["BUTT_WELD_EASE_M"] = BUTT_WELD_EASE_M
    ns.setdefault("weld_deck_butts", weld_deck_butts)

    tp_cls = ns.get("TunnelProfiles")
    if tp_cls is not None and hasattr(tp_cls, "_road_caps") and not getattr(tp_cls._road_caps, "_brf_wrapped", False):
        def _road_caps(self, xy, floor, th, ups, mouths, width, ground):
            import fast
            import roadmesh
            fast.require(45, "first_probe_near_lines")
            side = width / 2.0 + roadmesh.TUNNEL_SHOULDER + roadmesh.LOW_PORTAL_SIDE
            caps, ups_out, any_ = [None, None], list(ups), False
            lines, radius = [], []
            for gk, lns in ground.items():
                hw = self._hw.get(gk)
                if hw is None:
                    continue
                for ln in lns:
                    lines.append(np.asarray(ln, np.float64).reshape(-1, 2))
                    radius.append(hw)
            if not lines or len(xy) < 2:
                return None, ups_out
            pts = np.concatenate(lines)
            starts = np.concatenate([[0], np.cumsum([len(ln) for ln in lines])]).astype(np.int64)
            radius_arr = np.asarray(radius, np.float64)
            ds = np.arange(0.0, roadmesh.LOW_CAP_M + 0.25, 0.5)
            gap_m = float(ns.get("ROAD_CAP_GAP_M", 0.5))
            join_m = float(ns.get("ROAD_CAP_JOIN_M", 1.0))
            over_m = float(ns.get("ROAD_CAP_OVER_M", 0.4))
            for e, (at, nb) in enumerate(((0, 1), (-1, -2))):
                if not mouths[e]:
                    continue
                p = xy[at]
                tin = xy[nb] - p
                tin = tin / max(float(np.hypot(*tin)), 1e-9)
                nrm = np.array([-tin[1], tin[0]])
                mid = p + tin * ds[:, None]
                probes = np.stack([mid, mid + nrm * side, mid - nrm * side], 1).reshape(-1, 2)
                got = fast._ef.first_probe_near_lines(
                    pts, starts, radius_arr, gap_m,
                    (float(p[0]), float(p[1])), join_m, probes,
                )
                if got is not None:
                    i, hx, hy = got
                    depth = max(0.0, float(ds[i // 3]) - gap_m)
                    road_z = float(self._ground(np.array([[hx, hy]]))[0])
                    top = road_z + over_m - float(floor[at]) - th
                    ups_out[e] = float(max(0.15, min(ups_out[e], top)))
                    caps[e], any_ = depth, True
            return (tuple(caps) if any_ else None), ups_out

        _road_caps._brf_wrapped = True
        tp_cls._road_caps = _road_caps

    orig_bar = ns.get("block_arch_raise")
    if callable(orig_bar) and not getattr(orig_bar, "_brf_wrapped", False):
        @functools.wraps(orig_bar)
        def _wrapped_block_arch_raise(tunnels, cell_fn, frame, la, lo, z, box):
            out = orig_bar(tunnels, cell_fn, frame, la, lo, z, box)
            z_arr = np.asarray(z, np.float64).ravel()
            out_arr = np.asarray(out, np.float64).ravel()
            changed = np.flatnonzero(out_arr != z_arr)
            if not len(changed):
                return out
            try:
                import fast
                import mapcells
                import mapgeom
                x0, y0, x1, y1 = box
                cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), np.zeros(2))
                keys = mapcells.cells_for_bbox(float(cla.min()), float(clo.min()), float(cla.max()), float(clo.max()))
                cells = []
                for key in keys:
                    try:
                        cells.append(cell_fn(key))
                    except Exception:
                        cells.append(None)
                parts = tunnels.parts(cells, box)
                if not parts:
                    return out
                others = _other_ground_roads(ns, mapgeom, parts, cells, frame)
                if not others:
                    return out
                e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64).ravel(), np.asarray(lo, np.float64).ravel(), 0.0)
                e_arr, n_arr = np.asarray(e, np.float64), np.asarray(n, np.float64)
                oth = fast.Others(others)
                margin = float(ns.get("HOLE_ROAD_MARGIN", 1.0))
                fixed = out_arr.copy()
                for idx in changed:
                    if not oth.clear((float(e_arr[idx]), float(n_arr[idx])), None, margin):
                        fixed[idx] = z_arr[idx]
                return fixed.reshape(np.shape(z))
            except Exception:
                return out

        _wrapped_block_arch_raise._brf_wrapped = True
        ns["block_arch_raise"] = _wrapped_block_arch_raise

    ns["_BRF_V2_ROADS_HOOKED"] = True


def apply_grading_hooks(ns):
    """Apply verified abutment elevation agreement and ramp constants onto grading."""
    if ns.get("_BRF_V2_GRADING_HOOKED"):
        return
    ns["ABUT_AGREE_M"] = ABUT_AGREE_M
    ns["ABUT_LOW_M"] = ABUT_LOW_M
    ns["ABUT_SHOULDER_TAPER_M"] = ABUT_SHOULDER_TAPER_M
    ns["APPROACH_WALL_SIDE_RUN"] = APPROACH_WALL_SIDE_RUN
    ns["DECK_GROUND_CLIFF_M"] = DECK_GROUND_CLIFF_M
    ns["PIN_SNAP_M"] = PIN_SNAP_M
    ns["_finite"] = _finite
    ns["abutment_z"] = abutment_z
    ns["_BRF_V2_GRADING_HOOKED"] = True


def apply_roadmesh_hooks(ns):
    """Apply verified box-tunnel headroom and arch-cap parapet bounds onto roadmesh."""
    if ns.get("_BRF_V2_ROADMESH_HOOKED"):
        return
    ns["LOW_RISE"] = LOW_RISE
    ns["PIER_SHAFT_T"] = PIER_SHAFT_T

    orig_sec = ns.get("_tunnel_section")
    if callable(orig_sec) and not getattr(orig_sec, "_brf_wrapped", False):
        @functools.wraps(orig_sec)
        def _tunnel_section(hw, th=ns.get("TUNNEL_H", 6.8), low=False, box=False):
            if box:
                sh = float(ns.get("TUNNEL_SHOULDER", 0.5))
                vault = [(float(x), float(th)) for x in np.linspace(hw, -hw, 9)]
                return np.array(
                    [(hw - sh, 0.0), (hw, 0.0)] + vault + [(-hw, 0.0), (-hw + sh, 0.0)],
                    np.float64,
                )
            return orig_sec(hw, th, low=low)

        _tunnel_section._brf_wrapped = True
        ns["_tunnel_section"] = _tunnel_section

    orig_cap = ns.get("_arch_cap")
    if callable(orig_cap) and not getattr(orig_cap, "_brf_wrapped", False):
        @functools.wraps(orig_cap)
        def _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, side, top, th, origin, depth=None, side_mat=2):
            cap_over = float(ns.get("ARCH_CAP_OVER", 0.35))
            low_up = float(ns.get("LOW_PORTAL_UP", 0.8))
            target_top = min(float(th) + cap_over, float(top) - 0.05)
            if target_top <= float(th) + 0.12:
                return nv
            eff_top = target_top + (low_up - cap_over)
            return orig_cap(
                P, T, UV, M, nv, mid, u, z, at, k, side, eff_top, th, origin,
                depth=depth, side_mat=side_mat,
            )

        _arch_cap._brf_wrapped = True
        ns["_arch_cap"] = _arch_cap

    ns["_BRF_V2_ROADMESH_HOOKED"] = True


def apply_plancache_hooks(ns, addon_rev):
    """Stamp add-on cache revision onto plancache.for_grader so cached plans auto-invalidate."""
    if ns.get("_BRF_V2_PLANCACHE_HOOKED"):
        return
    orig_fg = ns.get("for_grader")
    if callable(orig_fg) and not getattr(orig_fg, "_brf_wrapped", False):
        @functools.wraps(orig_fg)
        def _for_grader(cache_dir, grader, widths, wet):
            pc = orig_fg(cache_dir, grader, widths, wet)
            if pc is not None and hasattr(pc, "signature"):
                tag = f"-brf{addon_rev}"
                if tag not in str(pc.signature):
                    pc.signature = f"{pc.signature}{tag}"
            return pc

        _for_grader._brf_wrapped = True
        ns["for_grader"] = _for_grader
    ns["_BRF_ADDON_REV"] = addon_rev
    ns["_BRF_V2_PLANCACHE_HOOKED"] = True
