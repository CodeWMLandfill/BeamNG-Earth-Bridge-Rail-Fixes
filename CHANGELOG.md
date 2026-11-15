# Changelog

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
