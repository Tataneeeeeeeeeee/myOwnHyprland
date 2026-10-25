-- Exécute une config Hyprland Lua avec une fausse API `hl` qui enregistre tout, puis imprime le résultat en JSON.
-- Usage : lua hl_mock.lua chemin/hyprland.lua

local entry = assert(arg[1], "usage: lua hl_mock.lua hyprland.lua")
local dir = entry:match("^(.*)/[^/]*$") or "."
package.path = dir .. "/?.lua;" .. dir .. "/?/init.lua;" .. package.path

local rec = {
    config = {}, binds = {}, window_rules = {}, layer_rules = {}, workspace_rules = {},
    env = {}, exec = {}, monitors = {}, curves = {}, animations = {}, gestures = {}, devices = {},
    events = {}, other = {}, errors = {},
}

local current_submap = ""

local function merge(dst, src)
    for k, v in pairs(src) do
        if type(v) == "table" and type(dst[k]) == "table" and #v == 0 then
            merge(dst[k], v)
        else
            dst[k] = v
        end
    end
end

local function handle()
    return setmetatable({}, { __index = function() return function() end end })
end

-- hl.dsp.window.move({...}) → { dsp = "window.move", args = {...} }
local function dsp_proxy(path)
    return setmetatable({}, {
        __index = function(_, k) return dsp_proxy(path == "" and k or (path .. "." .. k)) end,
        __call = function(_, ...) return { dsp = path, args = { ... } } end,
    })
end

local function push(list) return function(spec) list[#list + 1] = spec; return handle() end end

hl = setmetatable({
    config = function(t) merge(rec.config, t) end,
    bind = function(keys, action, opts)
        -- fichier:ligne de l'appel, pour pointer précisément un conflit de raccourcis
        local info = debug.getinfo(2, "Sl")
        local src = info and info.source and info.source:gsub("^@", "") or "?"
        local prefix = dir .. "/"
        if src:sub(1, #prefix) == prefix then src = src:sub(#prefix + 1) end
        rec.binds[#rec.binds + 1] = { keys = keys, action = action, opts = opts, submap = current_submap,
                                      src = src, line = info and info.currentline or 0 }
        return handle()
    end,
    unbind = function(keys)
        for i = #rec.binds, 1, -1 do
            local b = rec.binds[i]
            if b.keys == keys and b.submap == current_submap then table.remove(rec.binds, i) end
        end
    end,
    define_submap = function(name, reset_or_fn, fn)
        local f = type(reset_or_fn) == "function" and reset_or_fn or fn
        local previous = current_submap
        current_submap = name
        if type(f) == "function" then
            local ok, err = pcall(f)
            if not ok then rec.errors[#rec.errors + 1] = tostring(err) end
        end
        current_submap = previous
    end,
    window_rule = push(rec.window_rules),
    layer_rule = push(rec.layer_rules),
    workspace_rule = push(rec.workspace_rules),
    monitor = push(rec.monitors),
    animation = push(rec.animations),
    gesture = push(rec.gestures),
    device = push(rec.devices),
    curve = function(name, spec) rec.curves[name] = spec end,
    env = function(k, v) rec.env[k] = v end,
    exec_cmd = function(cmd) rec.exec[#rec.exec + 1] = cmd end,
    on = function(event, fn)
        rec.events[#rec.events + 1] = event
        -- On joue les callbacks de démarrage pour capter les hl.exec_cmd qu'ils contiennent
        if event == "hyprland.start" or event == "config.reloaded" then
            local ok, err = pcall(fn)
            if not ok then rec.errors[#rec.errors + 1] = tostring(err) end
        end
        return handle()
    end,
    dsp = dsp_proxy(""),
}, {
    -- Fonctions non modélisées (hl.notification, hl.get_*, …) : on enregistre l'appel et on renvoie un objet neutre
    __index = function(_, name)
        return setmetatable({}, {
            __call = function(_, ...) rec.other[#rec.other + 1] = name; return handle() end,
            __index = function() return function() return handle() end end,
        })
    end,
})

local before = {}
for k in pairs(_G) do before[k] = true end

local ok, err = pcall(dofile, entry)

-- Variables globales créées par la config (hors fonctions) : souvent une erreur silencieuse
rec.globals = {}
for k, v in pairs(_G) do
    if not before[k] and type(v) ~= "function" then rec.globals[#rec.globals + 1] = k end
end
if not ok then rec.errors[#rec.errors + 1] = tostring(err) end

-- Encodeur JSON minimal
local function encode(v, seen)
    local t = type(v)
    if t == "nil" then return "null"
    elseif t == "boolean" or t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        return tostring(v)
    elseif t == "string" then
        return '"' .. v:gsub('[%c"\\]', function(c)
            return ({ ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\t'] = '\\t', ['\r'] = '\\r' })[c]
                or string.format("\\u%04x", c:byte())
        end) .. '"'
    elseif t == "table" then
        seen = seen or {}
        if seen[v] then return "null" end
        seen[v] = true
        local n, out = #v, {}
        local is_array = n > 0 or next(v) == nil
        if is_array then
            for i = 1, n do out[i] = encode(v[i], seen) end
            seen[v] = nil
            return "[" .. table.concat(out, ",") .. "]"
        end
        for k, val in pairs(v) do
            out[#out + 1] = encode(tostring(k)) .. ":" .. encode(val, seen)
        end
        seen[v] = nil
        return "{" .. table.concat(out, ",") .. "}"
    end
    return '"<' .. t .. '>"'
end

io.write(encode(rec))
