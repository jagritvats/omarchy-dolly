-- Dolly input bridge for Hyprland's Lua config.
--
-- Loaded ONLY while a recording runs, via:
--   hyprctl eval 'dofile("<plugin>/hypr/dolly-input.lua")'
-- and torn down with:
--   hyprctl eval 'if dolly_input_stop then dolly_input_stop() end'
--
-- Why: clicks and keystrokes cannot be read from hyprctl or the event socket,
-- and /dev/input needs the `input` group. Hyprland itself can observe them:
--   * non-consuming, mod-agnostic mouse binds see every button press
--     (the click still reaches the app)
--   * the `input.keyboard.key` event sees every key (keycode, time, state)
-- Each observation is appended as one JSON line to
-- $XDG_RUNTIME_DIR/dolly/input.jsonl, which the Dolly logger tails and
-- timestamps on arrival.
-- Nothing is written anywhere when no recording is active.

if dolly_input_stop then pcall(dolly_input_stop) end

local runtime = os.getenv("XDG_RUNTIME_DIR") or "/tmp"
local path = runtime .. "/dolly/input.jsonl"
local f = io.open(path, "a")
if not f then return end
f:setvbuf("line")

local function emit(kind, extra)
  local pos = hl.get_cursor_pos() or { x = 0, y = 0 }
  if not f then return end
  f:write(string.format('{"type":"%s","x":%.2f,"y":%.2f%s}\n', kind, pos.x or 0, pos.y or 0, extra or ""))
end

local buttons = { [272] = "left", [273] = "right", [274] = "middle" }
local bound = {}
for code, name in pairs(buttons) do
  local key = "mouse:" .. code
  local ok = pcall(hl.bind, key, function() emit("click", ',"button":"' .. name .. '"') end,
    { non_consuming = true, ignore_mods = true, transparent = true, description = "dolly click observer" })
  if ok then bound[#bound + 1] = key end
end

-- key capture is skipped when the recorder created the "nokeys" marker
local nk = io.open(runtime .. "/dolly/nokeys", "r")
local KEY_ON = nk == nil
if nk then nk:close() end
local sub = nil
if KEY_ON then
  local ok, s = pcall(hl.on, "input.keyboard.key", function(keycode, _time, state)
    -- presses and releases (releases let the logger track held modifiers);
    -- auto-repeat (state 2) is dropped
    if f and state ~= 2 then
      f:write(string.format('{"type":"key","code":%d,"down":%s}\n', keycode, state == 1 and "true" or "false"))
    end
  end)
  if ok then sub = s end
end

f:write('{"type":"bridge","clicks":' .. #bound .. ',"keys":' .. tostring(sub ~= nil) .. '}\n')

function dolly_input_stop()
  for _, key in ipairs(bound) do pcall(hl.unbind, key) end
  bound = {}
  if sub and sub.remove then pcall(function() sub:remove() end) end
  sub = nil
  if f then pcall(function() f:close() end) end
  f = nil
  dolly_input_stop = nil
end
