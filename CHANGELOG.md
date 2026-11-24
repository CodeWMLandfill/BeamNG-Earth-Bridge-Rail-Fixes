# Changelog

## v0.4.0

### Highlights
- Expand official add-on scope and branding to **BeamNG.Earth - Bridge, Rail & Tunnel Fixes** (`v0.4.0`), adding **`tunnel`** to `Ctrl+Shift+B` spot-report categories (`bridge`, `rail`, `tunnel`, `road`, `other`) while keeping all option keys, state folders, and `beamng_earth_bridge_rail_fixes.py` asset filenames backward-compatible.
- Ship a **dual-payload `full_solver` architecture** supporting **BeamNG.Earth v1.9.1-beta.dev.424** through **v1.10.1-beta** (`direct` solver mode, `PLANS_FORMAT = 55`) and **v1.10.1-beta.dev.574** (`earthfast 621`, native `server_v2` overlay across `14` solver modules, `PLANS_FORMAT = 78`), while routing intermediate (`dev.567`) and unknown future ABIs safely to `client_and_reporter` (`client_only`) mode.
- Update surgical `roads.lua` patching (`createWalk_railcol`) so both `v1.10.1-beta` and `v1.10.1-beta.dev.574+` (`M.isCliffCollider(q.what)`) layouts patch cleanly and idempotently.
- Harden symmetric add-on stacking and shared telemetry collector coexistence so **Bridge, Rail & Tunnel Fixes** (`Ctrl+Shift+B`) and **Traffic and AutoDrive fixes** (`Ctrl+Shift+T`) install, run, and restore cleanly side-by-side in either order.

### Bridges, Ramps & Divided Corridors
- Smooth tall-abutment head-slope soffits, pair sustained parallel link-to-mainline decks, excavate buried-deck footprints, preserve cross-cell divided carriageway continuity, and trim terrain step-ribbons along split bridge approaches (`grading`, `carriageways`, `roads`, `roadmesh`, `bridgetypes`).

### Railways & Crossings
- Enforce graded railway bridge underpass clearance and motorway sleeper exclusion (`railways`, `railprofile`, `rails`).
- Suppress phantom steel rails and crossings on ground abandoned railway beds converted into collinear bikeways/trails while preserving all physically present tracks (`heritage`, `osm_rails=yes`, active `osm_electrified`, and tracks running beside street sidewalks).

### Tunnels, Portals & Multi-Bore Clearance
- Seal open-mouth portal collars and continuous portal floor decks, cap hillside portal scree skirts below natural ground/headwall lines, punch 24 m open-mouth portal throats across steep terrain cells, and prevent staggered or curved neighboring bores from clipping headwalls, wingwalls, or seam bulkheads into adjacent driving lanes (`portals`, `roadmesh`, `roads`, `tunnellids`).

## v0.3.1

### Highlights
- Support **BeamNG.Earth v1.9.1-beta.dev.424** through **v1.10.1-beta** in full solver mode (`full_solver`, `21` patched server modules + `roads.lua` + `Ctrl+Shift+B` reporter), and add automatic forward-compatible `client_and_reporter` mode (`patchTier`) for newer dev builds (`v1.10.1-beta.dev.491+` / `dev.567+`).
- Update surgical `roads.lua` patching (`reset_viewAt`) so both `v1.9.1-beta.dev.424..v1.10.1-beta` and `v1.10.1-beta.dev.567+` (`M._dropRetiring()`) layouts patch cleanly and idempotently.
- Capture active `BeamNG.Earth` experimental switch IDs (`experimentalFlags`) in `bridge_rail_report.py` via guarded `experimental` inspection (`read_settings` / `chosen`) while preserving strict path redaction and Worker backward compatibility.

## v0.3.0

### Highlights
- Support **BeamNG.Earth v1.9.1-beta.dev.424** through **v1.10.0-beta.dev.456** on 64-bit Windows 10 and 11 (`21` patched server modules including `fences`, `vegetation`, and `delineators`, with `PLANS_FORMAT = 54` and automatic add-on revision cache invalidation).
- Add a quiet one-time launch corner notice when a newer release of **Bridge & Rail Fixes** is available (`notifyAddonUpdates`), alongside an optional launch notice for previously reported spots (`notifyFixedReports`).
- Verify `1,269,747 / 8,949,779` structures clean (`14.19%`, `11 / 234` structural archetypes cleared) across `128 / 128` global Overture Maps shards.

### Bridges, Ramps, Piers & Guardrails
- Couple opposite-direction divided carriageways and twin overpass bridges across corridors to preserve full approach widths and prevent twin-slab elevation splits.
- Smooth curved ramp diverge approaches, behind-abutment head-slopes, and bridge-end apron transitions while preventing elevated spans from raising embankment fill underneath decks.
- Propagate cross-cell lane counts on full-bridge ways, enforce multi-tier stacked viaduct clearance, and suppress pier columns or portal caps over active lower lanes and markings.
- Render continuous 0.5 m sloped concrete highway noise barriers (`fences.noise_walls`), smooth non-parapet retaining wall faces, and clear utility poles and delineator posts from paved travel lanes and gores (`furniture`, `delineators`, `vegetation`).

### Railways, Tunnels & Streaming
- Align elevated light-rail and railway viaduct deck clearances over multi-level roadways (`railways`, `railprofile`).
- Gate pedestrian walkway bores, step parallel motorway tunnel mouths cleanly, and keep open-cut tunnel portal lids aligned (`tunnellids`).
- Stamp `_BRF_ADDON_REV` into `earthd`, `blockworker`, `plancache`, and `roads` so add-on updates automatically invalidate stale local plan and revisit caches without manual cache clearing.

### Installer & Add-on UX (`Ctrl+Shift+B`)
- Add `notifyAddonUpdates` (`Notify when an add-on update is available`, enabled by default) and `notifyFixedReports` (`Notify when my reported spots are fixed`, enabled by default) toggles in **Add-ons -> Options**, displaying non-focus-stealing corner notices only after world load settles.

## v0.2.0

### Highlights
- Support **BeamNG.Earth v1.9.1-beta.dev.424** through **v1.9.1-beta.dev.445** on 64-bit Windows 10 and 11.
- Ship as a single 1-click installer script (`beamng_earth_bridge_rail_fixes.py`) or direct GitHub add-on (`CodeWMLandfill/BeamNG-Earth-Bridge-Rail-Fixes`).
- Verify `240,613 / 8,949,779` structures clean (`2.69%`) across `128 / 128` global Overture Maps shards (`126,578` bridges, `58,295` railways, `55,740` tunnels).

### Bridges, Ramps & Abutments
- Fix abrupt approach cliffs and sunken deck profiles where bridges meet graded highway embankments.
- Propagate lane counts and deck widths across collinear ground-to-bridge ramp continuations so two-lane approaches do not narrow into single-lane bridge decks.
- Prevent dead-end viaduct stubs from sloping down onto active lower roadways.
- Stop elevated bridge spans and promoted viaduct stubs from raising graded terrain mounds underneath the bridge deck.
- Smooth secondary and diverging ramp bridge profiles at highway split gores instead of leaving steep humps or vertical steps.
- Skew bridge abutments cleanly across open-cut tunnel portals and underpasses.
- Weld collinear 1-to-1 free bridge deck ends and keep cross-cell motorway lane counts consistent across multi-hop cell borders.

### Piers, Trusses & Clearances
- Keep overpass pier columns and portal bents strictly outside lower travel lanes, ramps, and shoulders, omitting piers where a clean compact straddle is not possible.
- Eliminate oversized diagonal portal crossbeams across skewed multi-deck highway interchanges.
- Separate stacked multi-level bridge decks and break elevation solver stack cycles on complex interchanges.
- Align bascule, truss, arch, and footbridge vertical clearances so overhead steelwork and walkways clear active roadways.

### Divided Highways, Medians & Guardrails
- Tie parallel twin-carriageway bridge elevations across shared medians (`<= 6.0 m`) to eliminate vertical slab steps between adjacent decks.
- Extend shared concrete median barriers continuously across divided twin-carriageway bridges without mid-span gaps.
- Smooth opposite parapet height steps and protruding concrete wedges at highway diverge gores.
- Subdivide tapered barrier ends and suppress lane-penetrating or buried guardrails across splits, merges, and cross-corridors.
- Smooth tile-border retaining wall faces and cap heights along steep highway embankments.

### Railways, Tunnels & Utility Poles
- Keep physical meter-scale railway bridge spans, motorway crossing cuts, and underpass headroom aligned across tile boundaries.
- Isolate tunnel and bridge profile solvers onto raw ungraded terrain DEM to prevent recursive cell-plan deadlocks during world streaming.
- Clamp open-cut underpasses, isolate raw tunnel floor profiles from overhead bridge decks, and merge interior cover seams to eliminate tunnel portal terrain steps.
- Add `tunnellids` and `railprofile` server modules to patch coverage for consistent tunnel portal lids and railway grade transitions.
- Exclude paved highway gores and overhead bridge deck footprints from utility and powerline pole placement so poles do not stand in gores or pierce viaduct slabs.

### Streaming, Installer & Spot Reporter (`Ctrl+Shift+B`)
- Patch `roads.lua` surgically on install so bridge deck and rail collision fixes preserve newer upstream `BeamNG.Earth` client functions.
- Evict cached cross-cell road lane specs on `roadlanes.invalidate` so updated bridge widths apply across cell boundaries.
- Block `Ctrl+Shift+B` spot reports while the world is mid-teleport, hidden, or still streaming initial terrain and road meshes to prevent black-frame captures.
- Add backward-compatible `loading_incomplete` telemetry fallback in `bridge_rail_report.py` and `worker.js` while preserving path, EXIF, and metadata stripping.

## v0.1.0
- Separate collapsed multi-level highway bridge decks and restore 3D diagonal steel truss bracing.
- Move highway overpass piers out of active travel lanes onto medians and clear zones.
- Keep divided highway overpass decks and lane markings continuous across cross-streets and ramp forks.
- Add 1-click Windows install/uninstall scripts (`1_Click_Install.bat`, `1_Click_Uninstall.bat`, `beamng_earth_bridge_rail_fixes.py`) and zero-PII `Ctrl+Shift+B` spot reporting.
