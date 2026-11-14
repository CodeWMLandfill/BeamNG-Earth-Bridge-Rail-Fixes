-- 1-hotkey silent bug reporter for BeamNG-Earth-Bridge-Rail-Fixes (Ctrl+Shift+B).
-- Captures anonymized coordinates/telemetry + screenshot locally, notifies the patched earthd server
-- to publish cleanly to the Cloudflare Worker collector, and flashes a subtle non-blocking toast.
local M = {}

M.LEVEL_ID = 'beamng_earth'
M.ADDON_ID = 'beamng-earth-bridge-rail-fixes'
M.ADDON_VERSION = '0.1.0'
M.DIR = 'beamngEarth/bridge_rail_reports/'
M.POLL_TIMEOUT_S = 22

local im = nil
local ourLevel = false
local seq = 0
local pending = nil      -- { rel = ..., deadline = ... }
local toast, toastUntil = nil, 0
local delayedToast, delayedAt = nil, nil

local wallMs = require('beamngEarth/stopwatch').clock()
local function now() return wallMs() / 1000 end

local function onLevel()
  ourLevel = rawget(_G, 'getCurrentLevelIdentifier') ~= nil and getCurrentLevelIdentifier() == M.LEVEL_ID
end

M.onClientStartMission = onLevel
function M.onClientEndMission() ourLevel = false end
function M.onExtensionLoaded() if scenetree and scenetree.MissionGroup then onLevel() end end

local function showToast(msg, dur)
  toast = tostring(msg)
  toastUntil = now() + (dur or 4)
end

local function addonOptions()
  local api = rawget(_G, 'beamngEarth_api')
  if api and type(api.addonOptions) == 'function' then
    local ok, opts = pcall(api.addonOptions, M.ADDON_ID)
    if ok and type(opts) == 'table' then return opts end
  end
  return {}
end

local function sanitizeCategory(v)
  if v == 'bridge' or v == 'rail' or v == 'road' or v == 'other' then return v end
  return 'bridge'
end

local function loadPairPending(p)
  if type(p) ~= 'table' then return false end
  local done, want = tonumber(p[1]) or 0, tonumber(p[2]) or 0
  return want > 0 and done < want
end

-- Defer 1-hotkey reporting while terrain/roads are provisional or still streaming so a temporary
-- un-graded tile is never filed as a permanent road/bridge bug.
local function provisionalBlocker(core, stStatus, anchorId)
  if core then
    if type(core.groundReady) == 'function' then
      local okG, ready = pcall(core.groundReady)
      if okG and ready == false then
        return 'Wait for terrain to finish loading before reporting'
      end
    end
    local rawSt = nil
    if type(core._state) == 'function' then
      local okR, r = pcall(core._state)
      if okR and type(r) == 'table' then rawSt = r end
    end
    local pend = (rawSt and type(rawSt.pending) == 'table' and rawSt.pending.purpose)
      or (type(stStatus) == 'table' and stStatus.pending)
    if pend == 'teleport' or pend == 'resync' then
      return 'Wait for teleport terrain before reporting'
    end
    if rawSt and (rawSt.placeFrames ~= nil or rawSt.teleportRetry ~= nil
        or (rawSt.anchor ~= nil and rawSt.block == nil)) then
      return 'Wait for terrain placement before reporting'
    end
    local blk = (rawSt and rawSt.block) or (type(stStatus) == 'table' and stStatus.block)
    if type(blk) == 'table' and blk.provisional == true then
      return 'Wait for graded terrain block before reporting'
    end
    local lp = rawSt and type(rawSt.loadProgress) == 'table' and rawSt.loadProgress or nil
    if lp and (anchorId == nil or lp.anchorId == nil or lp.anchorId == anchorId) then
      if loadPairPending(lp.ground) then
        return 'Wait for graded first block before reporting'
      end
      if loadPairPending(lp.roads) then
        return 'Wait for road and bridge tiles before reporting'
      end
    end
  end

  local okLp, loadprogress = pcall(require, 'beamngEarth/loadprogress')
  if okLp and type(loadprogress) == 'table' and type(loadprogress.active) == 'function' then
    local okAct, active = pcall(loadprogress.active)
    if okAct and active == true then
      return 'Wait for initial world load to finish before reporting'
    end
  end

  local okRd, roadsMod = pcall(require, 'beamngEarth/roads')
  if okRd and type(roadsMod) == 'table' and type(roadsMod.queued) == 'function' then
    local okQ, q = pcall(roadsMod.queued)
    if okQ and type(q) == 'number' and q > 0 then
      return 'Wait for queued road tiles before reporting'
    end
  end
  return nil
end

function M.status()
  return {
    ourLevel = ourLevel,
    pending = pending ~= nil,
    toast = (toast and now() < toastUntil) and toast or nil,
  }
end

function M.captureSilent()
  if not ourLevel then return false, 'not_in_level' end
  local opts = addonOptions()
  if opts.enableOneHotkeyReport == false then
    local msg = '1-hotkey reporter disabled in Add-on Options'
    showToast(msg, 3)
    return false, msg
  end

  local core = rawget(_G, 'beamngEarth_core')
  local stStatus = nil
  local lat, lon, altM, anchorId, earthVer = nil, nil, nil, nil, nil
  if core and core.status then
    local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
    if ok and type(st) == 'table' then
      stStatus = st
      if type(st.player) == 'table' then
        lat, lon = st.player.lat, st.player.lon
        altM = st.player.alt or st.player.altM
      end
      anchorId = st.anchorId
    end
  end
  local api = rawget(_G, 'beamngEarth_api')
  if api and type(api.pose) == 'function' then
    local okP, p = pcall(api.pose)
    if okP and type(p) == 'table' then
      lat = lat or p.lat
      lon = lon or p.lon
      altM = altM or p.alt
    end
  end
  if api and type(api.earthVersion) == 'function' then
    local okV, ver = pcall(api.earthVersion)
    if okV and type(ver) == 'string' then earthVer = ver end
  end

  if type(lat) ~= 'number' or type(lon) ~= 'number' then
    local msg = 'Wait for world coordinates before reporting'
    showToast(msg, 3)
    return false, msg
  end

  local blocker = provisionalBlocker(core, stStatus, anchorId)
  if blocker then
    showToast(blocker, 4)
    return false, blocker
  end

  local headingDeg, speedMs = nil, nil
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local v, f = veh:getVelocity(), veh:getDirectionVector()
    speedMs = v and v:length() or nil
    if f then headingDeg = math.deg(math.atan2(f.x, f.y)) % 360 end
  end

  local cam = nil
  if core_camera and core_camera.getPosition then
    local c = core_camera.getPosition()
    local fw = core_camera.getForward and core_camera.getForward()
    if c then
      cam = { pos = { c.x, c.y, c.z }, forward = fw and { fw.x, fw.y, fw.z } or nil }
    end
  end

  seq = seq + 1
  local kind = sanitizeCategory(opts.reportCategory)
  local id = os.date('!%Y%m%d_%H%M%S') .. '_' .. tostring(seq)
  local rel = M.DIR .. id .. '/'
  if not FS:directoryExists(rel) then FS:directoryCreate(rel, true) end

  -- Clear any visible toast and take the screenshot cleanly before drawing status feedback.
  toast, toastUntil = nil, 0
  if type(createScreenshot2) == 'function' then
    pcall(createScreenshot2, { filename = rel .. 'screenshot', writeJPG = true, superSampling = 1 })
  end

  local payload = {
    lat = lat,
    lon = lon,
    kind = kind,
    summary = string.format('In-game %s report @ %.5f, %.5f', kind, lat, lon),
    note = 'Captured with Ctrl+Shift+B silent 1-hotkey reporter.',
    earthVersion = earthVer or (core and core.MOD_VERSION) or nil,
    addonVersion = M.ADDON_VERSION,
    headingDeg = headingDeg,
    altM = altM,
    speedMs = speedMs,
    anchorId = anchorId,
    camera = cam,
  }

  local okEnc, encoded = pcall(jsonEncode, payload)
  if okEnc and encoded then
    pcall(writeFile, rel .. 'report.json', encoded)
  end

  local okNet, net = pcall(require, 'beamngEarth/net')
  if okNet and net and type(net.send) == 'function' and net.isConnected and net.isConnected() then
    net.send({ type = 'bridge_rail_report', id = id, rel = rel, payload = payload })
    pending = { rel = rel, kind = kind, deadline = now() + M.POLL_TIMEOUT_S }
    delayedToast = 'Uploading ' .. kind .. ' report...'
    delayedAt = now() + 0.15
  else
    delayedToast = 'Saved report locally (world server disconnected)'
    delayedAt = now() + 0.15
  end
  return true
end

function M.onUpdate()
  local t = now()
  if delayedToast and delayedAt and t >= delayedAt then
    showToast(delayedToast, 3)
    delayedToast, delayedAt = nil, nil
  end
  if pending then
    local receipt = pending.rel .. 'uploaded.json'
    if FS and FS:fileExists(receipt) then
      local txt = readFile(receipt)
      local okD, res = pcall(jsonDecode, txt or '')
      if okD and type(res) == 'table' then
        if res.ok then
          showToast(string.format('Sent #%s (%s)', tostring(res.issueNumber or '?'), pending.kind), 5)
        else
          showToast('Saved locally (upload failed: ' .. tostring(res.error or 'error') .. ')', 5)
        end
      else
        showToast('Report uploaded', 4)
      end
      pending = nil
    elseif t > pending.deadline then
      pending = nil
    end
  end

  im = im or rawget(_G, 'ui_imgui')
  if not im or not toast or t >= toastUntil then return end
  im.SetNextWindowPos(im.ImVec2(20, 150), im.Cond_Always)
  if im.Begin('##bngeBridgeRailToast', nil,
      im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize + im.WindowFlags_NoInputs) then
    im.TextUnformatted(toast)
  end
  im.End()
end

return M
