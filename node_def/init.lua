local function serialize_value(value, indent, seen)
    local t = type(value)
    if t == "string" then
        return string.format("%q", value)
    elseif t == "number" or t == "boolean" then
        return tostring(value)
    elseif t == "table" then
        if seen[value] then
            return "nil"
        end
        seen[value] = true
        local parts = {"{\n"}
        local inner = indent .. "    "
        local is_array = true
        local max_index = 0
        for k, _ in pairs(value) do
            if type(k) ~= "number" or k ~= math.floor(k) or k < 1 then
                is_array = false
                break
            end
            if k > max_index then max_index = k end
        end
        if is_array and max_index > 0 then
            for i = 1, max_index do
                if value[i] ~= nil then
                    parts[#parts + 1] = inner
                    parts[#parts + 1] = serialize_value(value[i], inner, seen)
                    parts[#parts + 1] = ",\n"
                end
            end
        else
            for k, v in pairs(value) do
                parts[#parts + 1] = inner
                if type(k) == "string" and string.match(k, "^[%a_][%a%d_]*$") then
                    parts[#parts + 1] = k
                else
                    parts[#parts + 1] = "["
                    parts[#parts + 1] = serialize_value(k, inner, seen)
                    parts[#parts + 1] = "]"
                end
                parts[#parts + 1] = " = "
                parts[#parts + 1] = serialize_value(v, inner, seen)
                parts[#parts + 1] = ",\n"
            end
        end
        parts[#parts + 1] = indent
        parts[#parts + 1] = "}"
        return table.concat(parts)
    else
        return "nil"
    end
end

minetest.register_chatcommand("dumpnodes", {
    params = "",
    description = "Dump all node definitions to nodes.lua (Lua table format)",
    func = function(name, param)
        local parts = {"return {\n"}
        local count = 0
        for nodename, def in pairs(minetest.registered_nodes) do
            count = count + 1
            parts[#parts + 1] = string.format("    [%q] = {\n", nodename)
            for k, v in pairs(def) do
                local tp = type(v)
                if tp ~= "function" and tp ~= "userdata" then
                    parts[#parts + 1] = "        "
                    if type(k) == "string" and string.match(k, "^[%a_][%a%d_]*$") then
                        parts[#parts + 1] = k
                    else
                        parts[#parts + 1] = "["
                        parts[#parts + 1] = serialize_value(k, "        ", {})
                        parts[#parts + 1] = "]"
                    end
                    parts[#parts + 1] = " = "
                    parts[#parts + 1] = serialize_value(v, "        ", {})
                    parts[#parts + 1] = ",\n"
                end
            end
            parts[#parts + 1] = "    },\n"
        end
        parts[#parts + 1] = "}\n"
        local content = table.concat(parts)
        local path = minetest.get_worldpath() .. "/nodes.lua"
        if core.safe_file_write(path, content) then
            return true, string.format("Node definitions written to %s (%d nodes)", path, count)
        else
            return false, "Failed to write nodes.lua"
        end
    end
})
