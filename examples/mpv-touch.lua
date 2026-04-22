-- mpv-touch.lua: touch-friendly on-screen controls
--
-- Tap anywhere  → reveal the control bar
-- Tap a button  → act immediately (also reveals bar if hidden)
-- Auto-hides after HIDE_TIMEOUT seconds
--
-- Zone layout (bottom BAR_FRAC of the screen):
--   ┌─────────┬─────────────┬─────────┬──────────┐
--   │  Vol−   │ Play/Pause  │  Vol+   │  ✕ Stop  │
--   └─────────┴─────────────┴─────────┴──────────┘

local HIDE_TIMEOUT = 3    -- seconds until the bar auto-hides
local VOL_STEP     = 5    -- volume change per tap (percent)
local BAR_FRAC     = 0.20 -- fraction of screen height used by the bar

local overlay  = mp.create_osd_overlay('ass-events')
local visible  = false
local timer    = nil
local tap_x, tap_y = 0, 0

-- ── helpers ──────────────────────────────────────────────────────────────────

local function clamp(v, lo, hi)
    return math.max(lo, math.min(hi, v))
end

local function hide()
    visible = false
    overlay:remove()
end

local function schedule_hide()
    if timer then timer:kill() end
    timer = mp.add_timeout(HIDE_TIMEOUT, hide)
end

-- ── overlay drawing ───────────────────────────────────────────────────────────
--
-- ASS colour format is &HBBGGRR& (blue-green-red).
-- Alpha:  &H00& = fully opaque,  &HFF& = fully transparent.

local function draw()
    local w  = mp.get_property_number('osd-width')  or 1280
    local h  = mp.get_property_number('osd-height') or 720
    local paused = mp.get_property_bool('pause') or false
    local vol    = math.floor(mp.get_property_number('volume') or 100)

    local bar_h  = math.floor(h * BAR_FRAC)
    local bar_y  = h - bar_h
    local mid_y  = bar_y + bar_h / 2
    local q      = w / 4

    -- Font sizes scale with bar height
    local fs_big  = math.floor(bar_h * 0.38)
    local fs_icon = math.floor(bar_h * 0.50)  -- play/pause icon slightly larger
    local fs_sub  = math.floor(bar_h * 0.20)  -- sub-label (volume %)

    local play_sym = paused and '▶' or '⏸'
    local vol_pct  = tostring(vol) .. '%'

    -- Semi-transparent dark background bar (ASS drawing path).
    -- \an7\pos(0,0) anchors the coordinate system at screen top-left.
    local bg = string.format(
        '{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\1a&H60&\\p1}'
        .. 'm 0 %.0f l %.0f %.0f %.0f %.0f 0 %.0f{\\p0}',
        bar_y, w, bar_y, w, h, h)

    -- Subtle dividers between zones (thin 2-px-wide rectangles).
    local divs = ''
    local pad  = math.floor(bar_h * 0.15)
    for i = 1, 3 do
        divs = divs .. string.format(
            '{\\an7\\pos(0,0)\\bord0\\shad0\\1c&HFFFFFF&\\1a&HC0&\\p1}'
            .. 'm %.0f %.0f l %.0f %.0f %.0f %.0f %.0f %.0f{\\p0}\n',
            q*i-1, bar_y+pad,   q*i+1, bar_y+pad,
            q*i+1, h-pad,       q*i-1, h-pad)
    end

    -- Button labels (\N is an ASS hard line-break).
    -- Colours: white for all buttons; red-ish (&H4444FF& = R=FF,G=44,B=44) for Stop.
    local labels = string.format(
        -- Vol−  with current level below
        '{\\an5\\pos(%.0f,%.0f)\\fs%d\\bord2\\shad0\\1c&HFFFFFF&}Vol−\\N{\\fs%d}%s\n'
        -- Play / Pause
        .. '{\\an5\\pos(%.0f,%.0f)\\fs%d\\bord2\\shad0\\1c&HFFFFFF&}%s\n'
        -- Vol+  with current level below
        .. '{\\an5\\pos(%.0f,%.0f)\\fs%d\\bord2\\shad0\\1c&HFFFFFF&}Vol+\\N{\\fs%d}%s\n'
        -- Stop  (red tint)
        .. '{\\an5\\pos(%.0f,%.0f)\\fs%d\\bord2\\shad0\\1c&H4444FF&}✕  Stop',

        q*0.5, mid_y, fs_big,  fs_sub, vol_pct,
        q*1.5, mid_y, fs_icon, play_sym,
        q*2.5, mid_y, fs_big,  fs_sub, vol_pct,
        q*3.5, mid_y, fs_big)

    overlay.data = bg .. '\n' .. divs .. labels
    overlay:update()
end

-- ── tap handler ───────────────────────────────────────────────────────────────

local function on_tap(x, y)
    local w     = mp.get_property_number('osd-width')  or 1280
    local h     = mp.get_property_number('osd-height') or 720
    local bar_y = h - math.floor(h * BAR_FRAC)

    -- Always show the overlay on any tap.
    if not visible then
        visible = true
    end

    -- Act on button zone regardless of whether overlay was already visible.
    -- This lets a user tap the (memorised) button position for immediate response.
    if y >= bar_y then
        local zone = math.floor(x / (w / 4))  -- 0=Vol−  1=Play/Pause  2=Vol+  3=Stop
        if zone == 0 then
            local v = mp.get_property_number('volume') or 100
            mp.set_property_number('volume', clamp(v - VOL_STEP, 0, 100))
        elseif zone == 1 then
            mp.set_property_bool('pause', not (mp.get_property_bool('pause') or false))
        elseif zone == 2 then
            local v = mp.get_property_number('volume') or 100
            mp.set_property_number('volume', clamp(v + VOL_STEP, 0, 100))
        elseif zone == 3 then
            mp.command('quit')
            return  -- no point scheduling a hide after quit
        end
    end

    draw()
    schedule_hide()
end

-- ── input bindings ────────────────────────────────────────────────────────────

-- Capture position on finger-down; act on finger-up (avoids positional drift).
mp.add_key_binding('MOUSE_BTN0', 'touch-tap', function(e)
    if e.event == 'down' then
        tap_x = mp.get_property_number('mouse-pos/x') or 0
        tap_y = mp.get_property_number('mouse-pos/y') or 0
    elseif e.event == 'up' then
        on_tap(tap_x, tap_y)
    end
end, {complex = true})

-- Scroll wheel / two-finger swipe → volume (always active, no overlay needed).
mp.add_key_binding('WHEEL_UP',   'touch-vol-up',
    function()
        local v = mp.get_property_number('volume') or 100
        mp.set_property_number('volume', clamp(v + VOL_STEP, 0, 100))
    end)
mp.add_key_binding('WHEEL_DOWN', 'touch-vol-down',
    function()
        local v = mp.get_property_number('volume') or 100
        mp.set_property_number('volume', clamp(v - VOL_STEP, 0, 100))
    end)

-- ── property observers ────────────────────────────────────────────────────────
-- Refresh the bar when state changes externally (e.g. via Lounge API / IPC).

mp.observe_property('pause',  'bool',   function() if visible then draw() end end)
mp.observe_property('volume', 'number', function() if visible then draw() end end)
