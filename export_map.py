#!/usr/bin/env python3
"""
Minetest/Luanti map.sqlite to OBJ exporter.

Reads mapblocks from a Minetest world database, deserializes node data
from v28 (zlib) and v29 (zstd) serialization formats, and exports
the geometry as a Wavefront OBJ file with MTL materials.

Usage:
    python3 export_map.py --db map.sqlite --nodes nodes.lua [options]

Serialization formats:
    v29 (0x1D): zstd-compressed, contains embedded node name mappings.
    v28 (0x1C): 5-byte header + zlib-compressed, no embedded mappings.
                Non-zero nodes exported with deterministic colors.
                A v28_color_map.txt file is generated for identification.
    Other: Treated as air to prevent failures.
"""

import argparse
import hashlib
import os
import re
import sqlite3
import struct
import sys
import zlib
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    print("ERROR: zstandard package required. Install with: pip install zstandard")
    sys.exit(1)


def parse_lua_table(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.strip()
    if content.startswith('return'):
        content = content[6:].strip()
    if content.endswith(';'):
        content = content[:-1].strip()

    nodes = {}
    i = 0
    length = len(content)

    while i < length:
        m = re.search(r'\["([^"]+)"\]\s*=\s*\{', content[i:])
        if not m:
            break

        node_name = m.group(1)
        start = i + m.end()
        brace_depth = 1
        j = start

        while j < length and brace_depth > 0:
            if content[j] == '{':
                brace_depth += 1
            elif content[j] == '}':
                brace_depth -= 1
            j += 1

        props_str = content[start:j - 1]
        props = parse_lua_properties(props_str)
        nodes[node_name] = props
        i = j

    return nodes


def parse_lua_properties(text):
    props = {}
    i = 0
    length = len(text)
    found_any = False

    while i < length:
        while i < length and text[i] in ' \t\n\r,':
            i += 1
        if i >= length:
            break

        m = re.match(r'\["([^"]+)"\]\s*=\s*', text[i:])
        if m:
            key = m.group(1)
            i += m.end()
            found_any = True
        else:
            m = re.match(r'(\w+)\s*=\s*', text[i:])
            if m:
                key = m.group(1)
                i += m.end()
                found_any = True
            else:
                i += 1
                continue

        value, i = parse_lua_value(text, i)
        props[key] = value

    if not found_any and text.strip():
        return parse_lua_value_list(text)

    return props


def parse_lua_value_list(text):
    items = []
    i = 0
    length = len(text)

    while i < length:
        while i < length and text[i] in ' \t\n\r,':
            i += 1
        if i >= length:
            break
        value, i = parse_lua_value(text, i)
        if value is not None:
            items.append(value)

    return items


def parse_lua_value(text, i):
    length = len(text)

    while i < length and text[i] in ' \t\n\r':
        i += 1

    if i >= length:
        return None, i

    if text[i] == '"':
        i += 1
        end = text.index('"', i)
        value = text[i:end]
        return value, end + 1

    if text[i:i+4] == 'true':
        return True, i + 4
    if text[i:i+5] == 'false':
        return False, i + 5

    m = re.match(r'(-?\d+\.?\d*)', text[i:])
    if m:
        num_str = m.group(1)
        if '.' in num_str:
            return float(num_str), i + len(num_str)
        return int(num_str), i + len(num_str)

    if text[i] == '{':
        brace_depth = 1
        j = i + 1
        while j < length and brace_depth > 0:
            if text[j] == '{':
                brace_depth += 1
            elif text[j] == '}':
                brace_depth -= 1
            elif text[j] == '"':
                j = text.index('"', j + 1)
            j += 1
        inner = text[i + 1:j - 1]
        return parse_lua_table_content(inner), j

    return None, i + 1


def parse_lua_table_content(text):
    text = text.strip()
    if not text:
        return {}

    i = 0
    length = len(text)
    while i < length and text[i] in ' \t\n\r,':
        i += 1
    if i >= length:
        return {}

    if re.match(r'\["', text[i:]) or re.match(r'\w+\s*=', text[i:]):
        return parse_lua_properties(text)

    return parse_lua_value_list(text)


def name_to_color(name):
    h = hashlib.md5(name.encode('utf-8')).digest()
    return (max(h[0], 40) / 255.0, max(h[1], 40) / 255.0, max(h[2], 40) / 255.0)


def param0_to_color(param0):
    h = hashlib.md5(f"v28_{param0}".encode('utf-8')).digest()
    return (max(h[0], 40) / 255.0, max(h[1], 40) / 255.0, max(h[2], 40) / 255.0)


def decompress_blob(blob):
    if len(blob) < 1:
        return -1, None

    version = blob[0]

    if version == 0x1D:
        try:
            dctx = zstd.ZstdDecompressor()
            data = dctx.decompress(blob[1:], max_output_size=1 << 20)
            return version, data
        except Exception as e:
            print(f"  WARNING: zstd decompression failed: {e}", file=sys.stderr)
            return version, None

    elif version == 0x1C:
        if len(blob) > 6:
            try:
                data = zlib.decompress(blob[6:])
                return version, data
            except Exception as e:
                print(f"  WARNING: zlib decompression failed: {e}", file=sys.stderr)
                return version, None
        return version, None

    else:
        print(f"  WARNING: Unknown serialization version {version}, treating as air",
              file=sys.stderr)
        return version, None


def deserialize_v29(data):
    offset = 0

    data[offset]; offset += 1
    struct.unpack('>H', data[offset:offset+2])[0]; offset += 2
    struct.unpack('>I', data[offset:offset+4])[0]; offset += 4
    data[offset]; offset += 1
    num_mappings = struct.unpack('>H', data[offset:offset+2])[0]; offset += 2

    mapping = {}
    for _ in range(num_mappings):
        node_id = struct.unpack('>H', data[offset:offset+2])[0]; offset += 2
        name_len = struct.unpack('>H', data[offset:offset+2])[0]; offset += 2
        name = data[offset:offset+name_len].decode('utf-8'); offset += name_len
        mapping[node_id] = name

    content_width = data[offset]; offset += 1
    data[offset]; offset += 1

    total_nodes = 4096

    if content_width == 2:
        param0 = struct.unpack(f'>{total_nodes}H', data[offset:offset + total_nodes * 2])
        offset += total_nodes * 2
    else:
        param0 = struct.unpack(f'>{total_nodes}B', data[offset:offset + total_nodes])
        offset += total_nodes

    param1 = struct.unpack(f'>{total_nodes}B', data[offset:offset + total_nodes])
    offset += total_nodes
    param2 = struct.unpack(f'>{total_nodes}B', data[offset:offset + total_nodes])

    nodes = {}
    for z in range(16):
        for y in range(16):
            for x in range(16):
                idx = z * 256 + y * 16 + x
                p0 = param0[idx]
                name = mapping.get(p0, f'unknown_{p0}')
                if name == 'air':
                    continue
                nodes[(x, y, z)] = {
                    'param0': p0,
                    'param1': param1[idx],
                    'param2': param2[idx],
                    'name': name,
                }

    return {'version': 29, 'mapping': mapping, 'nodes': nodes}


def deserialize_v28(data):
    total_nodes = 4096
    expected_size = total_nodes * 4

    if len(data) < expected_size:
        print(f"  WARNING: v28 data too short ({len(data)} < {expected_size})",
              file=sys.stderr)
        return {'version': 28, 'mapping': {}, 'nodes': {}}

    offset = 0
    param0 = struct.unpack(f'>{total_nodes}H', data[offset:offset + total_nodes * 2])
    offset += total_nodes * 2
    param1 = struct.unpack(f'>{total_nodes}B', data[offset:offset + total_nodes])
    offset += total_nodes
    param2 = struct.unpack(f'>{total_nodes}B', data[offset:offset + total_nodes])

    nodes = {}
    for z in range(16):
        for y in range(16):
            for x in range(16):
                idx = z * 256 + y * 16 + x
                p0 = param0[idx]
                if p0 != 0:
                    nodes[(x, y, z)] = {
                        'param0': p0,
                        'param1': param1[idx],
                        'param2': param2[idx],
                        'name': f'v28_unknown_{p0}',
                    }

    return {'version': 28, 'mapping': {}, 'nodes': nodes}


def _process_block_row(args):
    """Module-level worker for parallel block deserialization (Pool-picklable)."""
    bx, by, bz, blob = args
    version, decompressed = decompress_blob(blob)
    if decompressed is None:
        return None
    if version == 0x1D:
        result = deserialize_v29(decompressed)
    elif version == 0x1C:
        result = deserialize_v28(decompressed)
    else:
        return None
    if not result['nodes']:
        return None
    return (bx, by, bz, result)


def query_blocks(db_path, min_mb, max_mb):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT x, y, z, data FROM blocks
        WHERE x >= ? AND x <= ?
          AND y >= ? AND y <= ?
          AND z >= ? AND z <= ?
        ORDER BY y, z, x
    """, (min_mb[0], max_mb[0], min_mb[1], max_mb[1], min_mb[2], max_mb[2]))

    rows = list(cur)
    conn.close()

    total = len(rows)
    # Convert to plain tuples with bytes blobs — sqlite3.Row is not picklable
    args_list = [(row['x'], row['y'], row['z'], bytes(row['data'])) for row in rows]

    with Pool() as pool:
        raw_results = pool.map(_process_block_row, args_list, chunksize=64)

    with_nodes = sum(1 for r in raw_results if r is not None)
    print(f"\nBlock statistics:")
    print(f"  Total queried: {total}")
    print(f"  With nodes:    {with_nodes}")
    print(f"  Empty/failed:  {total - with_nodes}")

    for r in raw_results:
        if r is not None:
            yield r


FACE_DIRS = [
    ( 0,  1,  0, 'top'),
    ( 0, -1,  0, 'bottom'),
    ( 1,  0,  0, 'east'),
    (-1,  0,  0, 'west'),
    ( 0,  0,  1, 'south'),
    ( 0,  0, -1, 'north'),
]

FACE_NAME_BY_INDEX = {
    0: 'top',
    1: 'bottom',
    2: 'east',
    3: 'west',
    4: 'south',
    5: 'north',
}

FACEDIR_TO_TILE_INDICES = [
    [0, 1, 2, 3, 4, 5],
    [0, 1, 4, 5, 3, 2],
    [0, 1, 3, 2, 5, 4],
    [0, 1, 5, 4, 2, 3],
    [5, 4, 2, 3, 0, 1],
    [2, 3, 4, 5, 0, 1],
    [4, 5, 3, 2, 0, 1],
    [3, 2, 5, 4, 0, 1],
    [4, 5, 2, 3, 1, 0],
    [3, 2, 4, 5, 1, 0],
    [5, 4, 3, 2, 1, 0],
    [2, 3, 5, 4, 1, 0],
    [3, 2, 0, 1, 4, 5],
    [5, 4, 0, 1, 3, 2],
    [2, 3, 0, 1, 5, 4],
    [4, 5, 0, 1, 2, 3],
    [2, 3, 1, 0, 4, 5],
    [4, 5, 1, 0, 3, 2],
    [3, 2, 1, 0, 5, 4],
    [5, 4, 1, 0, 2, 3],
    [1, 0, 3, 2, 4, 5],
    [1, 0, 5, 4, 3, 2],
    [1, 0, 2, 3, 5, 4],
    [1, 0, 4, 5, 2, 3],
]

FACEDIR_TO_TILE_ROTATIONS = [
    [0, 0, 0, 0, 0, 0],
    [1, 3, 0, 0, 0, 0],
    [2, 2, 0, 0, 0, 0],
    [3, 1, 0, 0, 0, 0],
    [0, 2, 1, 3, 2, 0],
    [0, 2, 1, 3, 3, 3],
    [0, 2, 1, 3, 0, 2],
    [0, 2, 1, 3, 1, 1],
    [2, 0, 3, 1, 2, 0],
    [2, 0, 3, 1, 1, 1],
    [2, 0, 3, 1, 0, 2],
    [2, 0, 3, 1, 3, 3],
    [1, 1, 1, 1, 3, 1],
    [1, 1, 2, 0, 3, 1],
    [1, 1, 3, 3, 3, 1],
    [1, 1, 0, 2, 3, 1],
    [3, 3, 3, 3, 1, 3],
    [3, 3, 2, 0, 1, 3],
    [3, 3, 1, 1, 1, 3],
    [3, 3, 0, 2, 1, 3],
    [2, 2, 2, 2, 2, 2],
    [1, 3, 2, 2, 2, 2],
    [0, 0, 2, 2, 2, 2],
    [3, 1, 2, 2, 2, 2],
]

FACE_QUADS = {
    'top':    [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)],
    'bottom': [(0, 0, 1), (0, 0, 0), (1, 0, 0), (1, 0, 1)],
    'east':   [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)],
    'west':   [(0, 0, 1), (0, 1, 1), (0, 1, 0), (0, 0, 0)],
    'south':  [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
    'north':  [(1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)],
}

FACE_NORMALS = {
    'top':    (0, 1, 0),
    'bottom': (0, -1, 0),
    'east':   (1, 0, 0),
    'west':   (-1, 0, 0),
    'south':  (0, 0, 1),
    'north':  (0, 0, -1),
}

FACE_UVS = {
    'top':    [(0, 0), (0, 1), (1, 1), (1, 0)],
    'bottom': [(1, 1), (1, 0), (0, 0), (0, 1)],
    'east':   [(0, 0), (0, 1), (1, 1), (1, 0)],
    'west':   [(1, 1), (1, 0), (0, 0), (0, 1)],
    'south':  [(0, 0), (1, 0), (1, 1), (0, 1)],
    'north':  [(1, 0), (0, 0), (0, 1), (1, 1)],
}


class MeshExporter:

    def __init__(self, node_defs, v28_param0_colors, texture_resolver=None, tex_output_dir=None):
        self.node_defs = node_defs
        self.v28_param0_colors = v28_param0_colors
        self.texture_resolver = texture_resolver
        self.tex_output_dir = tex_output_dir
        self.grid = {}
        self.full_blocks = {}
        self.materials = {}
        self.material_textures = {}
        self.material_has_alpha = {}
        self.material_faces = defaultdict(list)
        self.vertices = []
        self.vert_index = {}
        self.normals = []
        self.norm_index = {}
        self.texcoords = []
        self.texcoord_index = {}
        self._resolved_tex_cache = {}
        self._mesh_index = {}
        self._safe_name_map = {}
        # Numpy-backed spatial structures — built once at the start of generate_mesh.
        # _vert_arr:         int32[x,y,z] -> 1-based OBJ vertex index (0 = unassigned).
        # _solid_arr:        bool[x,y,z]  -> True for full solid blocks (excl. flowingliquid).
        #                    Used by normal blocks and the fast path to cull faces.
        # _liquid_solid_arr: bool[x,y,z]  -> True for solid + any liquid (source or flowing).
        #                    Used by liquid blocks only so they cull faces at liquid boundaries.
        # _arr_offset:       (ox,oy,oz)   — world_coord = array_index + offset.
        self._vert_arr = None
        self._solid_arr = None
        self._liquid_solid_arr = None
        self._arr_offset = (0, 0, 0)
        self._arr_shape = (0, 0, 0)
        self._build_mesh_index()

    def _build_safe_name_map(self):
        used = set()
        for mat_name in self.materials:
            base = re.sub(r'[^a-zA-Z0-9_]', '_', mat_name)
            safe = base
            counter = 0
            while safe in used:
                counter += 1
                safe = f"{base}_{counter}"
            used.add(safe)
            self._safe_name_map[mat_name] = safe

    def _build_mesh_index(self):
        if not self.texture_resolver:
            return
        checked_dirs = set()
        for tex_dir in self.texture_resolver.texture_dirs:
            # Check parent directory (mod root) for models/ sibling
            parent_dir = os.path.dirname(tex_dir)
            if parent_dir not in checked_dirs:
                checked_dirs.add(parent_dir)
                models_dir = os.path.join(parent_dir, 'models')
                if os.path.isdir(models_dir):
                    for f in os.listdir(models_dir):
                        if f.endswith('.obj'):
                            self._mesh_index[f] = os.path.join(models_dir, f)
            # Also walk inside texture dirs for nested models/
            for root, dirs, files in os.walk(tex_dir):
                if 'models' in dirs:
                    m_dir = os.path.join(root, 'models')
                    for f in os.listdir(m_dir):
                        if f.endswith('.obj'):
                            self._mesh_index[f] = os.path.join(m_dir, f)
        self._tex_counter = 0

    def add_block(self, bx, by, bz, block_data):
        for (lx, ly, lz), node_info in block_data['nodes'].items():
            gx = bx * 16 + lx
            gy = by * 16 + ly
            gz = bz * 16 + lz
            self.grid[(gx, gy, gz)] = node_info
            node_name = node_info.get('name', '')
            node_def = self.node_defs.get(node_name, {})
            drawtype = node_def.get('drawtype', 'normal')
            self.full_blocks[(gx, gy, gz)] = self._is_full_block(drawtype, node_def)

    def _is_full_block(self, drawtype, node_def):
        if drawtype in ('normal', 'liquid'):
            return True
        return False

    def get_material_name(self, node_info, face_idx=None):
        name = node_info['name']
        face_suffix = FACE_NAME_BY_INDEX.get(face_idx, str(face_idx)) if face_idx is not None else None
        mat_key = f"{name}_{face_suffix}" if face_suffix is not None else name
        if mat_key in self.materials:
            return mat_key

        if name.startswith('v28_unknown_'):
            param0 = node_info['param0']
            if param0 in self.v28_param0_colors:
                color = self.v28_param0_colors[param0]
            else:
                color = param0_to_color(param0)
                self.v28_param0_colors[param0] = color
            self.materials[mat_key] = color
        else:
            color = name_to_color(name)
            self.materials[mat_key] = color
            self._resolve_node_texture(mat_key, name, node_info, face_idx)
        return mat_key

    def _resolve_node_texture(self, mat_key, node_name, node_info, face_idx=None):
        if not self.texture_resolver or not self.tex_output_dir:
            return
        if mat_key in self._resolved_tex_cache:
            self.material_textures[mat_key] = self._resolved_tex_cache[mat_key]
            return

        node_def = self.node_defs.get(node_name, {})
        tiles = node_def.get('tiles', [])

        tex_str = None
        if tiles and len(tiles) > 0:
            tile = None
            if face_idx is not None:
                if face_idx < len(tiles):
                    tile = tiles[face_idx]
                elif len(tiles) >= 3 and face_idx >= 2:
                    tile = tiles[2]
                else:
                    tile = tiles[-1]
            else:
                tile = tiles[0]
            if isinstance(tile, str):
                tex_str = tile
            elif isinstance(tile, dict):
                tex_str = tile.get('name', '')

        if not tex_str or tex_str == 'unknown':
            self._resolved_tex_cache[mat_key] = None
            return

        try:
            from texture_system import resolve_texture
            img = resolve_texture(tex_str, self.texture_resolver)
        except ImportError:
            self._resolved_tex_cache[mat_key] = None
            return

        if img is None:
            self._resolved_tex_cache[mat_key] = None
            return

        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', mat_key)
        tex_filename = f"{safe_name}.png"
        tex_path = os.path.join(self.tex_output_dir, tex_filename)

        try:
            img.save(tex_path)
            self._resolved_tex_cache[mat_key] = tex_filename
            self.material_textures[mat_key] = tex_filename
            if img.mode == 'RGBA':
                alpha_min = img.split()[3].getextrema()[0]
                self.material_has_alpha[mat_key] = alpha_min < 255
            else:
                self.material_has_alpha[mat_key] = False
        except Exception:
            self._resolved_tex_cache[mat_key] = None

    def get_vert_index(self, pos):
        if pos not in self.vert_index:
            self.vertices.append(pos)
            self.vert_index[pos] = len(self.vertices)
        return self.vert_index[pos]

    def get_norm_index(self, normal):
        if normal not in self.norm_index:
            self.normals.append(normal)
            self.norm_index[normal] = len(self.normals)
        return self.norm_index[normal]

    def get_texcoord_index(self, uv):
        if uv not in self.texcoord_index:
            self.texcoords.append(uv)
            self.texcoord_index[uv] = len(self.texcoords)
        return self.texcoord_index[uv]

    def emit_face(self, node_pos, face_name, material_name):
        nx, ny, nz = node_pos
        quad = FACE_QUADS[face_name]
        normal = FACE_NORMALS[face_name]

        vert_indices = []
        va = self._vert_arr
        if va is not None:
            ox, oy, oz = self._arr_offset
            verts = self.vertices
            for vx, vy, vz in quad:
                ix = nx + vx - ox
                iy = ny + vy - oy
                iz = nz + vz - oz
                existing = int(va[ix, iy, iz])
                if existing:
                    vert_indices.append(existing)
                else:
                    idx = len(verts) + 1
                    va[ix, iy, iz] = idx
                    verts.append((nx + vx, ny + vy, nz + vz))
                    vert_indices.append(idx)
        else:
            for vx, vy, vz in quad:
                vert_indices.append(self.get_vert_index((nx + vx, ny + vy, nz + vz)))

        uvs = FACE_UVS[face_name]
        uv_indices = [self.get_texcoord_index((u, v)) for u, v in uvs]

        norm_idx = self.get_norm_index(normal)
        face_str = ' '.join(f'{vi}/{ui}/{norm_idx}'
                           for vi, ui in zip(vert_indices, uv_indices))
        self.material_faces[material_name].append(face_str)

    def emit_quad(self, node_pos, verts, normal, material_name, uvs=None):
        nx, ny, nz = node_pos
        vert_indices = []
        for vx, vy, vz in verts:
            pos = (nx + vx, ny + vy, nz + vz)
            vert_indices.append(self.get_vert_index(pos))
        if uvs is None:
            uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
        uv_indices = [self.get_texcoord_index((u, v)) for u, v in uvs]
        norm_idx = self.get_norm_index(normal)
        face_str = ' '.join(f'{vi}/{ui}/{norm_idx}'
                           for vi, ui in zip(vert_indices, uv_indices))
        self.material_faces[material_name].append(face_str)

    def _rotate_facedir(self, x, y, z, param2, center=(0.5, 0.5, 0.5)):
        cx, cy, cz = center
        rx, ry, rz = x - cx, y - cy, z - cz

        facedir = param2 % 24
        axis_id = facedir // 4
        spin = facedir % 4

        if spin == 1:
            rx, rz = rz, -rx
        elif spin == 2:
            rx, rz = -rx, -rz
        elif spin == 3:
            rx, rz = -rz, rx

        if axis_id == 1:
            rx, ry, rz = rx, -rz, ry
        elif axis_id == 2:
            rx, ry, rz = rx, rz, -ry
        elif axis_id == 3:
            rx, ry, rz = ry, -rx, rz
        elif axis_id == 4:
            rx, ry, rz = -ry, rx, rz
        elif axis_id == 5:
            rx, ry, rz = -rx, -ry, rz

        return (rx + cx, ry + cy, rz + cz)

    def emit_box(self, node_pos, box, material_name, param2=0, node_info=None):
        x1, y1, z1, x2, y2, z2 = box
        x1, y1, z1 = x1 + 0.5, y1 + 0.5, z1 + 0.5
        x2, y2, z2 = x2 + 0.5, y2 + 0.5, z2 + 0.5
        if param2 and param2 % 24 != 0:
            corners = [(x1, y1, z1), (x2, y1, z1), (x2, y1, z2), (x1, y1, z2),
                       (x1, y2, z1), (x2, y2, z1), (x2, y2, z2), (x1, y2, z2)]
            rotated = [self._rotate_facedir(*c, param2) for c in corners]
            x1 = min(c[0] for c in rotated)
            x2 = max(c[0] for c in rotated)
            y1 = min(c[1] for c in rotated)
            y2 = max(c[1] for c in rotated)
            z1 = min(c[2] for c in rotated)
            z2 = max(c[2] for c in rotated)
        facedir = param2 % 24 if param2 else 0
        node_def = self.node_defs.get(node_info['name'], {}) if node_info else {}
        tiles_list = node_def.get('tiles', [])
        num_tiles = len(tiles_list)
        nx, ny, nz = node_pos
        box_faces = [
            ((x1, y2, z1), (x1, y2, z2), (x2, y2, z2), (x2, y2, z1), (0, 1, 0)),
            ((x1, y1, z2), (x1, y1, z1), (x2, y1, z1), (x2, y1, z2), (0, -1, 0)),
            ((x2, y1, z1), (x2, y2, z1), (x2, y2, z2), (x2, y1, z2), (1, 0, 0)),
            ((x1, y1, z2), (x1, y2, z2), (x1, y2, z1), (x1, y1, z1), (-1, 0, 0)),
            ((x1, y1, z2), (x2, y1, z2), (x2, y2, z2), (x1, y2, z2), (0, 0, 1)),
            ((x2, y1, z1), (x1, y1, z1), (x1, y2, z1), (x2, y2, z1), (0, 0, -1)),
        ]
        for face_idx, (v0, v1, v2, v3, normal) in enumerate(box_faces):
            if normal[1] != 0:
                uvs = [(v[0] - x1, v[2] - z1) for v in (v0, v1, v2, v3)]
            elif normal[0] != 0:
                uvs = [(v[2] - z1, v[1] - y1) for v in (v0, v1, v2, v3)]
            else:
                uvs = [(v[0] - x1, v[1] - y1) for v in (v0, v1, v2, v3)]
            raw_tile_idx = FACEDIR_TO_TILE_INDICES[facedir][face_idx]
            tile_idx = min(raw_tile_idx, num_tiles - 1) if num_tiles > 0 else raw_tile_idx
            rot = FACEDIR_TO_TILE_ROTATIONS[facedir][face_idx]
            if rot != 0:
                uc = sum(u for u, _ in uvs) / 4
                vc = sum(v for _, v in uvs) / 4
                ruvs = []
                for u, v in uvs:
                    du, dv = u - uc, v - vc
                    if rot == 1:
                        ruvs.append((uc - dv, vc + du))
                    elif rot == 2:
                        ruvs.append((uc - du, vc - dv))
                    else:
                        ruvs.append((uc + dv, vc - du))
                uvs = ruvs
            if node_info is not None:
                face_mat = self.get_material_name(node_info, tile_idx)
            else:
                face_mat = material_name
            self.emit_quad(node_pos, [v0, v1, v2, v3], normal, face_mat, uvs)

    def emit_node_cube(self, node_pos, material_name, drawtype, min_node, max_node, node_info=None):
        gx, gy, gz = node_pos
        always_show = drawtype in ('glasslike', 'allfaces', 'allfaces_optional',
                                    'glasslike_framed', 'glasslike_framed_optional')

        # Precompute material for single-tile nodes — one lookup instead of up to six.
        if node_info is not None:
            tiles = self.node_defs.get(node_info['name'], {}).get('tiles', [])
            multi_tile = len(tiles) > 1
            single_mat = None if multi_tile else self.get_material_name(node_info, 0)
        else:
            multi_tile = False
            single_mat = material_name

        # Use solid_arr for O(1) array indexing instead of two dict lookups per face.
        # Source liquid blocks use liquid_solid_arr so they also cull faces toward
        # flowing liquid neighbours (avoiding a one-sided barrier face at the boundary).
        if drawtype == 'liquid' and self._liquid_solid_arr is not None:
            solid = self._liquid_solid_arr
        else:
            solid = self._solid_arr
        if solid is not None:
            ox, oy, oz = self._arr_offset
            sx, sy, sz = self._arr_shape
            aix = gx - ox
            aiy = gy - oy
            aiz = gz - oz
            for face_idx, (dx, dy, dz, face_name) in enumerate(FACE_DIRS):
                if not always_show:
                    nix, niy, niz = aix + dx, aiy + dy, aiz + dz
                    if (0 <= nix < sx and 0 <= niy < sy and 0 <= niz < sz
                            and solid[nix, niy, niz]):
                        continue
                face_material = self.get_material_name(node_info, face_idx) if multi_tile else single_mat
                self.emit_face(node_pos, face_name, face_material)
        else:
            # Fallback: original dict-based path
            grid = self.grid
            full_blocks = self.full_blocks
            for face_idx, (dx, dy, dz, face_name) in enumerate(FACE_DIRS):
                neighbor_pos = (gx + dx, gy + dy, gz + dz)
                if not always_show:
                    if neighbor_pos in grid and full_blocks.get(neighbor_pos, False):
                        continue
                face_material = self.get_material_name(node_info, face_idx) if multi_tile else single_mat
                self.emit_face(node_pos, face_name, face_material)

    # ------------------------------------------------------------------
    # Flowing-liquid helpers
    # ------------------------------------------------------------------

    def _get_liquid_level(self, pos):
        """
        Classify any grid position by its liquid level.

        Returns
        -------
        8    – source block (drawtype 'liquid')
        0-7  – flowing block level encoded in param2 bits 0-2
        -1   – solid / opaque block (blocks light spread)
        None – air or position absent from the grid
        """
        node_info = self.grid.get(pos)
        if node_info is None:
            return None
        node_def = self.node_defs.get(node_info.get('name', ''), {})
        drawtype  = node_def.get('drawtype', 'normal')
        if drawtype == 'liquid':
            return 8
        if drawtype == 'flowingliquid':
            return (node_info.get('param2', 0) or 0) & 0x07
        if drawtype == 'airlike':
            return None
        return -1  # any other solid block

    def _get_liquid_corner_height(self, gx, gy, gz, dx, dz):
        """
        Interpolated surface height at one top corner, matching Luanti's
        getCornerLevel algorithm (mapblock_mesh.cpp).

        Parameters
        ----------
        dx, dz : 0 or 1
            Which corner of the block along the x and z axes.

        Returns
        -------
        float in [0.0, 1.0] where 1.0 == top of block.

        Algorithm (mirrors Luanti source):
          1. For each of the 4 grid cells sharing this corner:
               a. If any liquid (source or flowing) exists at (pos.y+1) above
                  that cell, return 1.0 immediately — liquid is pouring down.
               b. Source block          → return 1.0 immediately.
               c. Flowing block         → accumulate level for average.
               d. Air or solid block    → skip (do NOT reduce corner height).
                  Solid walls are excluded from the average; they don't pull
                  the surface down.  This matches Luanti behaviour where water
                  running along a wall stays at its natural level.
          2. If any source was found → 1.0.
          3. If any flowing blocks were found → (average_level + 0.5) / 8.
          4. Fallback → 0 (shouldn't occur; the block itself is always included).
        """
        cx = 1 if dx == 1 else -1
        cz = 1 if dz == 1 else -1
        positions = [
            (gx,      gy, gz     ),
            (gx + cx, gy, gz     ),
            (gx,      gy, gz + cz),
            (gx + cx, gy, gz + cz),
        ]
        valid_levels = []
        has_source   = False
        for pos in positions:
            # ── Check if liquid is present in the block directly above ──────
            # If so, liquid is flowing downward into this corner → full height.
            above_lv = self._get_liquid_level((pos[0], pos[1] + 1, pos[2]))
            if above_lv is not None and above_lv != -1:
                return 1.0

            lv = self._get_liquid_level(pos)
            if lv is None or lv == -1:
                continue        # air or solid wall → skip, do not reduce height
            if lv == 8:
                has_source = True
            else:
                valid_levels.append(lv)

        if has_source:
            return 1.0
        if valid_levels:
            return min(1.0, (sum(valid_levels) / len(valid_levels) + 0.5) / 8.0)
        return 0.0  # fallback; unreachable in practice since self is always counted

    def emit_flowing_liquid(self, node_pos, material_name, node_info=None):
        """
        param2 encoding (Luanti source: mapnode.h):
          bits 0-2  (& 0x07)  liquid level 0-7
          bit  3    (& 0x08)  is_flowing_down — flat top at own level

        Corner heights are interpolated from surrounding liquid levels to
        produce sloped surfaces.  Side face v-coordinates are scaled to the
        corner heights (texture clipped, not stretched).

        Face index → tile index follows FACE_DIRS order:
          0 top, 1 bottom, 2 east(+x), 3 west(-x), 4 south(+z), 5 north(-z)
        """
        gx, gy, gz     = node_pos
        param2         = (node_info.get('param2', 0) or 0) if node_info else 0
        is_flowing_down = bool(param2 & 0x08)
        level           = param2 & 0x07

        # ── Per-face material lookup ──────────────────────────────────────────
        if node_info is not None:
            tiles      = self.node_defs.get(node_info['name'], {}).get('tiles', [])
            multi_tile = len(tiles) > 1
            def face_mat(fi):
                return self.get_material_name(node_info, fi) if multi_tile else material_name
        else:
            def face_mat(_fi):
                return material_name

        # ── Neighbour-solid test ───────────────────────────────────────────────
        # Uses _liquid_solid_arr (solid blocks + all liquid) so that:
        #   flowing → solid block       : face culled
        #   flowing → source liquid     : face culled  (no barrier at transition)
        #   flowing → flowing liquid    : face culled  (no interior duplicate faces)
        #   flowing → air / other       : face shown 
        liq_solid = self._liquid_solid_arr
        if liq_solid is not None:
            ox, oy, oz = self._arr_offset
            sx, sy, sz = self._arr_shape
            def should_show(nx, ny, nz):
                nix, niy, niz = nx - ox, ny - oy, nz - oz
                if not (0 <= nix < sx and 0 <= niy < sy and 0 <= niz < sz):
                    return True     # out of bounds → exposed face
                return not liq_solid[nix, niy, niz]
        else:
            grid        = self.grid
            full_blocks = self.full_blocks
            def should_show(nx, ny, nz):
                pos = (nx, ny, nz)
                if pos not in grid:
                    return True
                node_def = self.node_defs.get(grid[pos].get('name', ''), {})
                dt = node_def.get('drawtype', 'normal')
                # cull toward any liquid or any full solid block
                if dt in ('liquid', 'flowingliquid'):
                    return False
                return not full_blocks.get(pos, False)

        # ── Corner heights ────────────────────────────────────────────────────
        if is_flowing_down:
            # Check if liquid is present directly above — if so the column is
            # continuous and the top must be flush at 1.0, not (level+0.5)/8.
            above_lv = self._get_liquid_level((gx, gy + 1, gz))
            if above_lv is not None and above_lv != -1:
                h00 = h10 = h01 = h11 = 1.0
            else:
                h = min(1.0, (level + 0.5) / 8.0)
                h00 = h10 = h01 = h11 = h
        else:
            # Interpolated corner heights; dx/dz = 0 → -x/-z corner, 1 → +x/+z
            h00 = self._get_liquid_corner_height(gx, gy, gz, 0, 0)  # x=0, z=0
            h10 = self._get_liquid_corner_height(gx, gy, gz, 1, 0)  # x=1, z=0
            h01 = self._get_liquid_corner_height(gx, gy, gz, 0, 1)  # x=0, z=1
            h11 = self._get_liquid_corner_height(gx, gy, gz, 1, 1)  # x=1, z=1

        # ── Top face (face index 0) ───────────────────────────────────────────
        # Vertices ordered to match FACE_QUADS['top']: (0,_,0),(0,_,1),(1,_,1),(1,_,0)
        if should_show(gx, gy + 1, gz):
            top_quad = [(0, h00, 0), (0, h01, 1), (1, h11, 1), (1, h10, 0)]
            self.emit_quad(node_pos, top_quad, (0, 1, 0),
                           face_mat(0), FACE_UVS['top'])

        # ── Bottom face (face index 1) ────────────────────────────────────────
        if should_show(gx, gy - 1, gz):
            self.emit_quad(node_pos, FACE_QUADS['bottom'],
                           (0, -1, 0), face_mat(1), FACE_UVS['bottom'])

        # ── East face +x (face index 2) ──────────────────────────────────────
        # Top edge: h10 at z=0, h11 at z=1.  UV v=0 at bottom → v=h at top.
        if should_show(gx + 1, gy, gz):
            eq = [(1, 0, 0), (1, h10, 0), (1, h11, 1), (1, 0, 1)]
            self.emit_quad(node_pos, eq, (1, 0, 0), face_mat(2),
                           [(0, 0), (0, h10), (1, h11), (1, 0)])

        # ── West face -x (face index 3) ──────────────────────────────────────
        # Top edge: h01 at z=1, h00 at z=0.  FACE_UVS['west'] has v=1 at bottom,
        # v=0 at top so we invert: bottom v=1, top v=1-h.
        if should_show(gx - 1, gy, gz):
            wq = [(0, 0, 1), (0, h01, 1), (0, h00, 0), (0, 0, 0)]
            self.emit_quad(node_pos, wq, (-1, 0, 0), face_mat(3),
                           [(1, 1), (1, 1 - h01), (0, 1 - h00), (0, 1)])

        # ── South face +z (face index 4) ─────────────────────────────────────
        # Top edge: h01 at x=0, h11 at x=1.  UV v=0 at bottom → v=h at top.
        if should_show(gx, gy, gz + 1):
            sq = [(0, 0, 1), (1, 0, 1), (1, h11, 1), (0, h01, 1)]
            self.emit_quad(node_pos, sq, (0, 0, 1), face_mat(4),
                           [(0, 0), (1, 0), (1, h11), (0, h01)])

        # ── North face -z (face index 5) ─────────────────────────────────────
        # Top edge: h10 at x=1, h00 at x=0.  UV v=0 at bottom → v=h at top.
        if should_show(gx, gy, gz - 1):
            nq = [(1, 0, 0), (0, 0, 0), (0, h00, 0), (1, h10, 0)]
            self.emit_quad(node_pos, nq, (0, 0, -1), face_mat(5),
                           [(1, 0), (0, 0), (0, h00), (1, h10)])

    def _load_obj_file(self, obj_path):
        vertices = []
        texcoords = []
        normals = []
        groups = []
        current_group = {'name': 'default', 'faces': []}

        with open(obj_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                cmd = parts[0]
                if cmd == 'v':
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                elif cmd == 'vt':
                    texcoords.append((float(parts[1]), float(parts[2])))
                elif cmd == 'vn':
                    normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
                elif cmd == 'g':
                    if current_group['faces']:
                        groups.append(current_group)
                    current_group = {'name': parts[1] if len(parts) > 1 else 'default', 'faces': []}
                elif cmd == 'f':
                    face_verts = []
                    for fp in parts[1:]:
                        indices = fp.split('/')
                        vi = int(indices[0]) - 1
                        ti = int(indices[1]) - 1 if len(indices) > 1 and indices[1] else -1
                        ni = int(indices[2]) - 1 if len(indices) > 2 and indices[2] else -1
                        face_verts.append((vi, ti, ni))
                    current_group['faces'].append(face_verts)
        if current_group['faces']:
            groups.append(current_group)
        return vertices, texcoords, normals, groups

    def emit_mesh_obj(self, node_pos, mesh_path, node_name, node_def, param2):
        if not os.path.isfile(mesh_path):
            return
        try:
            vertices, texcoords, normals, groups = self._load_obj_file(mesh_path)
        except Exception:
            return
        nx, ny, nz = node_pos
        visual_scale = node_def.get('visual_scale', 1.0)
        if visual_scale is None:
            visual_scale = 1.0
        tiles = node_def.get('tiles', [])
        group_materials = []
        for gi in range(len(groups)):
            tile = tiles[gi] if gi < len(tiles) else (tiles[-1] if tiles else None)
            if isinstance(tile, dict):
                tile_name = tile.get('name', '')
            elif isinstance(tile, str):
                tile_name = tile
            else:
                tile_name = ''
            mat_name = f"{node_name}_g{gi}"
            self.materials[mat_name] = (1.0, 1.0, 1.0)
            if tile_name and self.texture_resolver:
                from texture_system import resolve_texture
                try:
                    img = resolve_texture(tile_name, self.texture_resolver)
                    if img and self.tex_output_dir:
                        safe = re.sub(r'[^a-zA-Z0-9_]', '_', mat_name)
                        tex_file = f"{safe}.png"
                        tex_path = os.path.join(self.tex_output_dir, tex_file)
                        img.save(tex_path)
                        self.material_textures[mat_name] = tex_file
                        if img.mode == 'RGBA':
                            alpha_min = img.split()[3].getextrema()[0]
                            self.material_has_alpha[mat_name] = alpha_min < 255
                except Exception:
                    pass
            group_materials.append(mat_name)
        for gi, group in enumerate(groups):
            mat_name = group_materials[gi] if gi < len(group_materials) else group_materials[-1]
            for face_verts in group['faces']:
                if len(face_verts) < 3:
                    continue
                obj_verts = []
                obj_uvs = []
                for vi, ti, ni in face_verts:
                    if 0 <= vi < len(vertices):
                        vx, vy, vz = vertices[vi]
                        vx *= visual_scale
                        vy *= visual_scale
                        vz *= visual_scale
                        if param2 and param2 % 24 != 0:
                            vx, vy, vz = self._rotate_facedir(vx, vy, vz, param2,
                                                               center=(0.0, 0.0, 0.0))
                        obj_verts.append((nx + vx + 0.5, ny + vy + 0.5, nz + vz + 0.5))
                    else:
                        obj_verts.append((nx, ny, nz))
                    if 0 <= ti < len(texcoords):
                        obj_uvs.append(texcoords[ti])
                    else:
                        obj_uvs.append((0, 0))
                if len(obj_verts) >= 3:
                    v0, v1, v2 = obj_verts[0], obj_verts[1], obj_verts[2]
                    e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
                    e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
                    nx_c = e1[1]*e2[2] - e1[2]*e2[1]
                    ny_c = e1[2]*e2[0] - e1[0]*e2[2]
                    nz_c = e1[0]*e2[1] - e1[1]*e2[0]
                    length = (nx_c*nx_c + ny_c*ny_c + nz_c*nz_c) ** 0.5
                    if length > 0:
                        normal = (nx_c/length, ny_c/length, nz_c/length)
                    else:
                        normal = (0, 1, 0)
                    vert_indices = [self.get_vert_index(v) for v in obj_verts]
                    uv_indices = [self.get_texcoord_index(uv) for uv in obj_uvs]
                    norm_idx = self.get_norm_index(normal)
                    for i in range(1, len(obj_verts) - 1):
                        tri = [0, i, i + 1]
                        face_str = ' '.join(
                            f'{vert_indices[j]}/{uv_indices[j]}/{norm_idx}' for j in tri)
                        self.material_faces[mat_name].append(face_str)

    def _find_mesh_file(self, mesh_name):
        if not mesh_name:
            return None
        if mesh_name in self._mesh_index:
            return self._mesh_index[mesh_name]
        if os.path.isfile(mesh_name):
            return mesh_name
        return None

    def _parse_nodeboxes(self, node_def):
        nb = node_def.get('node_box', {})
        nb_type = nb.get('type', 'regular')
        if nb_type == 'regular':
            return [(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)]
        if nb_type == 'fixed':
            fixed = nb.get('fixed', [])
            if isinstance(fixed, dict):
                return [tuple(fixed)]
            if isinstance(fixed, list) and fixed:
                if isinstance(fixed[0], (int, float)):
                    return [tuple(fixed)]
                return [tuple(b) for b in fixed]
            return [(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)]
        if nb_type == 'wallmounted':
            return [(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)]
        if nb_type == 'connected':
            fixed = nb.get('fixed', [])
            if isinstance(fixed, dict):
                return [tuple(fixed)]
            if isinstance(fixed, list) and fixed:
                if isinstance(fixed[0], (int, float)):
                    return [tuple(fixed)]
                return [tuple(b) for b in fixed]
            return [(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)]
        return [(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)]

    def _get_plantlike_planes(self, param2, paramtype2):
        planes = []
        if paramtype2 == 'meshoptions' and param2:
            mode = param2 % 8
            if mode == 0:
                planes = [
                    [(-0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)],
                    [(-0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, 0.5)],
                ]
            elif mode == 1:
                planes = [
                    [(0, -0.5, -0.5), (0, -0.5, 0.5), (0, 0.5, 0.5), (0, 0.5, -0.5)],
                    [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)],
                ]
            elif mode == 2:
                planes = [
                    [(-0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)],
                    [(-0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, 0.5)],
                    [(0, -0.5, -0.5), (0, -0.5, 0.5), (0, 0.5, 0.5), (0, 0.5, -0.5)],
                    [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)],
                ]
            elif mode == 3:
                planes = [
                    [(-0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)],
                    [(-0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, 0.5)],
                    [(0, -0.5, -0.5), (0, -0.5, 0.5), (0, 0.5, 0.5), (0, 0.5, -0.5)],
                    [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)],
                    [(-0.25, -0.5, -0.25), (0.25, -0.5, 0.25), (0.25, 0.5, 0.25), (-0.25, 0.5, -0.25)],
                    [(-0.25, -0.5, 0.25), (0.25, -0.5, -0.25), (0.25, 0.5, -0.25), (-0.25, 0.5, 0.25)],
                ]
            else:
                planes = [
                    [(-0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)],
                    [(-0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, 0.5)],
                ]
        else:
            planes = [
                [(-0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)],
                [(-0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, 0.5)],
            ]
        return planes

    def generate_mesh(self, min_node, max_node):
        import numpy as np

        total_nodes = len(self.grid)
        print(f"\nGenerating mesh from {total_nodes} nodes...")
        if not self.grid:
            return

        # ── Build world-space bounding box ────────────────────────────────────
        all_pos = list(self.grid.keys())
        min_gx = min(p[0] for p in all_pos); max_gx = max(p[0] for p in all_pos)
        min_gy = min(p[1] for p in all_pos); max_gy = max(p[1] for p in all_pos)
        min_gz = min(p[2] for p in all_pos); max_gz = max(p[2] for p in all_pos)
        ox = min_gx - 1; oy = min_gy - 1; oz = min_gz - 1
        shape = (max_gx - ox + 2, max_gy - oy + 2, max_gz - oz + 2)
        self._arr_offset = (ox, oy, oz)
        self._arr_shape  = shape

        # ── Build solid-block array ───────────────────────────────────────────
        solid_arr = np.zeros(shape, dtype=np.bool_)
        for pos, is_full in self.full_blocks.items():
            if is_full:
                solid_arr[pos[0]-ox, pos[1]-oy, pos[2]-oz] = True
        self._solid_arr = solid_arr

        # ── Build liquid-solid array (solid + any liquid) ─────────────────────
        # Used exclusively by liquid blocks so they cull faces at liquid–liquid
        # and liquid–solid boundaries without leaking into normal-block culling.
        liquid_solid_arr = solid_arr.copy()
        for pos, node_info in self.grid.items():
            node_def = self.node_defs.get(node_info.get('name', ''), {})
            dt = node_def.get('drawtype', 'normal')
            if dt == 'flowingliquid':
                liquid_solid_arr[pos[0]-ox, pos[1]-oy, pos[2]-oz] = True
        self._liquid_solid_arr = liquid_solid_arr

        # ── Build vertex-index array ──────────────────────────────────────────
        vert_arr = np.zeros(shape, dtype=np.int32)
        self._vert_arr = vert_arr

        # ── Pre-register UVs and normals for all 6 cube face orientations ─────
        face_uv_idx   = {}
        face_norm_idx = {}
        for dx, dy, dz, face_name in FACE_DIRS:
            face_uv_idx[face_name]   = tuple(
                self.get_texcoord_index(uv) for uv in FACE_UVS[face_name]
            )
            face_norm_idx[face_name] = self.get_norm_index(FACE_NORMALS[face_name])

        # ── Classify nodes into fast / slow paths ─────────────────────────────
        ALWAYS_SHOW_DT = frozenset((
            'glasslike', 'allfaces', 'allfaces_optional',
            'glasslike_framed', 'glasslike_framed_optional',
        ))

        fast_cull   = {}   # mat -> [(x,y,z), ...] normal single-tile, needs culling
        fast_always = {}   # mat -> [(x,y,z), ...] glasslike single-tile, always emit
        slow_nodes  = []   # (pos, node_info, node_def, drawtype)

        processed = 0
        for pos, node_info in self.grid.items():
            processed += 1
            if processed % 200_000 == 0:
                print(f"  Classifying: {processed * 100 // total_nodes}%", end='\r')

            node_name = node_info['name']
            node_def  = self.node_defs.get(node_name, {})
            drawtype  = node_def.get('drawtype', 'normal')

            if drawtype == 'airlike':
                continue

            if drawtype == 'normal' or drawtype in ALWAYS_SHOW_DT:
                tiles = node_def.get('tiles', [])
                if len(tiles) <= 1:
                    mat    = self.get_material_name(node_info, 0)
                    bucket = fast_always if drawtype in ALWAYS_SHOW_DT else fast_cull
                    bucket.setdefault(mat, []).append(pos)
                    continue

            slow_nodes.append((pos, node_info, node_def, drawtype))

        n_fast = (sum(len(v) for v in fast_cull.values())
                  + sum(len(v) for v in fast_always.values()))
        print(f"  Fast path: {n_fast} nodes  |  Slow path: {len(slow_nodes)} nodes")

        # ── Vectorized fast path ──────────────────────────────────────────────
        def _emit_cube_batch(mat_to_pos, always_show):
            for mat, positions in mat_to_pos.items():
                pos_arr = np.array(positions, dtype=np.int32)  # (N, 3)
                aix = pos_arr[:, 0] - ox
                aiy = pos_arr[:, 1] - oy
                aiz = pos_arr[:, 2] - oz

                face_list = self.material_faces[mat]
                verts     = self.vertices

                for dx, dy, dz, face_name in FACE_DIRS:
                    if always_show:
                        mask = np.ones(len(pos_arr), dtype=np.bool_)
                    else:
                        nix = aix + dx
                        niy = aiy + dy
                        niz = aiz + dz
                        in_bounds = (
                            (nix >= 0) & (nix < shape[0]) &
                            (niy >= 0) & (niy < shape[1]) &
                            (niz >= 0) & (niz < shape[2])
                        )
                        is_solid = np.zeros(len(pos_arr), dtype=np.bool_)
                        if in_bounds.any():
                            is_solid[in_bounds] = solid_arr[
                                nix[in_bounds], niy[in_bounds], niz[in_bounds]
                            ]
                        mask = ~is_solid

                    if not mask.any():
                        continue

                    eaix = aix[mask]
                    eaiy = aiy[mask]
                    eaiz = aiz[mask]

                    quad            = FACE_QUADS[face_name]
                    ui0,ui1,ui2,ui3 = face_uv_idx[face_name]
                    ni              = face_norm_idx[face_name]
                    s0 = f'/{ui0}/{ni} '
                    s1 = f'/{ui1}/{ni} '
                    s2 = f'/{ui2}/{ni} '
                    s3 = f'/{ui3}/{ni}'

                    vert_idxs = []
                    for vx, vy, vz in quad:
                        vix = eaix + vx
                        viy = eaiy + vy
                        viz = eaiz + vz

                        raw       = vert_arr[vix, viy, viz]
                        zero_mask = raw == 0

                        if zero_mask.any():
                            new_stack = np.stack(
                                [vix[zero_mask], viy[zero_mask], viz[zero_mask]], axis=1
                            )
                            unique_new, inverse = np.unique(
                                new_stack, axis=0, return_inverse=True
                            )
                            n_new  = len(unique_new)
                            start  = len(verts) + 1
                            new_vi = np.arange(start, start + n_new, dtype=np.int32)
                            vert_arr[
                                unique_new[:, 0],
                                unique_new[:, 1],
                                unique_new[:, 2],
                            ] = new_vi
                            for ux, uy, uz in unique_new:
                                verts.append((int(ux)+ox, int(uy)+oy, int(uz)+oz))
                            raw = vert_arr[vix, viy, viz]

                        vert_idxs.append(raw)

                    vi0_l = vert_idxs[0].tolist()
                    vi1_l = vert_idxs[1].tolist()
                    vi2_l = vert_idxs[2].tolist()
                    vi3_l = vert_idxs[3].tolist()
                    face_list.extend(
                        f'{a}{s0}{b}{s1}{c}{s2}{d}{s3}'
                        for a, b, c, d in zip(vi0_l, vi1_l, vi2_l, vi3_l)
                    )

        _emit_cube_batch(fast_cull,   always_show=False)
        _emit_cube_batch(fast_always, always_show=True)

        # ── Slow path ─────────────────────────────────────────────────────────
        total_slow = len(slow_nodes)
        if total_slow:
            print(f"  Slow path processing...")
        for idx_s, (pos, node_info, node_def, drawtype) in enumerate(slow_nodes):
            if idx_s % 50_000 == 0 and idx_s:
                print(f"  Slow path: {idx_s * 100 // total_slow}%", end='\r')

            gx, gy, gz    = pos
            material_name = self.get_material_name(node_info)

            if drawtype == 'mesh':
                mesh_name = node_def.get('mesh')
                param2    = node_info.get('param2', 0) or 0
                mesh_path = self._find_mesh_file(mesh_name)
                if mesh_path:
                    self.emit_mesh_obj(pos, mesh_path, node_info['name'], node_def, param2)
                else:
                    self.emit_node_cube(pos, material_name, 'normal',
                                        min_node, max_node, node_info=node_info)
                continue

            if drawtype == 'nodebox':
                param2     = node_info.get('param2', 0) or 0
                paramtype2 = node_def.get('paramtype2', '')
                if paramtype2 != 'facedir':
                    param2 = 0
                for box in self._parse_nodeboxes(node_def):
                    self.emit_box(pos, box, material_name, param2, node_info=node_info)
                continue

            if drawtype in ('plantlike', 'plantlike_rooted'):
                param2     = node_info.get('param2', 0) or 0
                paramtype2 = node_def.get('paramtype2', '')
                for plane_verts in self._get_plantlike_planes(param2, paramtype2):
                    e1 = (plane_verts[1][0]-plane_verts[0][0],
                          plane_verts[1][1]-plane_verts[0][1],
                          plane_verts[1][2]-plane_verts[0][2])
                    e2 = (plane_verts[2][0]-plane_verts[0][0],
                          plane_verts[2][1]-plane_verts[0][1],
                          plane_verts[2][2]-plane_verts[0][2])
                    nx_c = e1[1]*e2[2] - e1[2]*e2[1]
                    ny_c = e1[2]*e2[0] - e1[0]*e2[2]
                    nz_c = e1[0]*e2[1] - e1[1]*e2[0]
                    length = (nx_c*nx_c + ny_c*ny_c + nz_c*nz_c) ** 0.5
                    normal = (nx_c/length, ny_c/length, nz_c/length) if length > 0 else (0, 1, 0)
                    self.emit_quad(pos, plane_verts, normal, material_name)
                continue

            if drawtype == 'torchlike':
                param2 = node_info.get('param2', 0) or 0
                if param2 == 0:
                    planes = [
                        [(-0.1,-0.5,0),(0.1,-0.5,0),(0.1,0.3,0),(-0.1,0.3,0)],
                        [(0,-0.5,-0.1),(0,-0.5,0.1),(0,0.3,0.1),(0,0.3,-0.1)],
                    ]
                elif param2 == 1:
                    planes = [
                        [(-0.1,-0.3,0),(0.1,-0.3,0),(0.1,0.5,0),(-0.1,0.5,0)],
                        [(0,-0.3,-0.1),(0,-0.3,0.1),(0,0.5,0.1),(0,0.5,-0.1)],
                    ]
                else:
                    planes = [[(-0.3,-0.1,0),(0.3,-0.1,0),(0.3,0.1,0),(-0.3,0.1,0)]]
                for plane_verts in planes:
                    self.emit_quad(pos, plane_verts, (0, 1, 0), material_name)
                continue

            if drawtype == 'signlike':
                self.emit_quad(
                    pos,
                    [(-0.5,-0.5,0.4),(0.5,-0.5,0.4),(0.5,0.5,0.4),(-0.5,0.5,0.4)],
                    (0, 0, 1), material_name,
                )
                continue

            if drawtype == 'firelike':
                planes = [
                    [(-0.5,-0.5,-0.1),(0.5,-0.5,0.1),(0.5,0.5,0.1),(-0.5,0.5,-0.1)],
                    [(-0.5,-0.5,0.1),(0.5,-0.5,-0.1),(0.5,0.5,-0.1),(-0.5,0.5,0.1)],
                    [(-0.1,-0.5,-0.5),(0.1,-0.5,0.5),(0.1,0.5,0.5),(-0.1,0.5,-0.5)],
                    [(-0.1,-0.5,0.5),(0.1,-0.5,-0.5),(0.1,0.5,-0.5),(-0.1,0.5,0.5)],
                ]
                for plane_verts in planes:
                    e1 = (plane_verts[1][0]-plane_verts[0][0],
                          plane_verts[1][1]-plane_verts[0][1],
                          plane_verts[1][2]-plane_verts[0][2])
                    e2 = (plane_verts[2][0]-plane_verts[0][0],
                          plane_verts[2][1]-plane_verts[0][1],
                          plane_verts[2][2]-plane_verts[0][2])
                    nx_c = e1[1]*e2[2] - e1[2]*e2[1]
                    ny_c = e1[2]*e2[0] - e1[0]*e2[2]
                    nz_c = e1[0]*e2[1] - e1[1]*e2[0]
                    length = (nx_c*nx_c + ny_c*ny_c + nz_c*nz_c) ** 0.5
                    normal = (nx_c/length, ny_c/length, nz_c/length) if length > 0 else (0, 1, 0)
                    self.emit_quad(pos, plane_verts, normal, material_name)
                continue

            if drawtype == 'flowingliquid':
                self.emit_flowing_liquid(pos, material_name, node_info=node_info)
                continue

            # liquid, multi-tile normal, and any other cube-like drawtype
            self.emit_node_cube(pos, material_name, drawtype,
                                min_node, max_node, node_info=node_info)

        print(f"  Mesh generation complete.")

    def write_obj(self, output_path):
        obj_path = output_path.with_suffix('.obj')
        mtl_name = obj_path.with_suffix('.mtl').name

        self._build_safe_name_map()

        total_faces = sum(len(faces) for faces in self.material_faces.values())
        print(f"\nWriting {obj_path.name}:")
        print(f"  Vertices:  {len(self.vertices)}")
        print(f"  Normals:   {len(self.normals)}")
        print(f"  Texcoords: {len(self.texcoords)}")
        print(f"  Faces:     {total_faces}")
        print(f"  Materials: {len(self.materials)}")

        # Change 1: 1 MB write buffer + writelines generators instead of
        # one f.write() per vertex/face (was 36M+ individual write syscalls).
        with open(obj_path, 'w', buffering=1 << 20) as f:
            f.write("# Minetest map export\n")
            f.write(f"mtllib {mtl_name}\n\n")

            f.writelines(f"v {x} {y} {z}\n" for x, y, z in self.vertices)
            f.write("\n")
            f.writelines(f"vn {nx} {ny} {nz}\n" for nx, ny, nz in self.normals)
            f.write("\n")
            f.writelines(f"vt {u} {v}\n" for u, v in self.texcoords)
            f.write("\n")

            for mat_name, faces in self.material_faces.items():
                if not faces:
                    continue
                safe_name = self._safe_name_map.get(mat_name, re.sub(r'[^a-zA-Z0-9_]', '_', mat_name))
                f.write(f"usemtl {safe_name}\n")
                f.writelines(f"f {face_str}\n" for face_str in faces)

        return obj_path

    def write_mtl(self, output_path):
        mtl_path = output_path.with_suffix('.mtl')

        with open(mtl_path, 'w') as f:
            f.write("# Minetest map export - Materials\n\n")

            for mat_name, (r, g, b) in sorted(self.materials.items()):
                safe_name = self._safe_name_map.get(mat_name, re.sub(r'[^a-zA-Z0-9_]', '_', mat_name))
                f.write(f"newmtl {safe_name}\n")

                tex_file = self.material_textures.get(mat_name)
                if tex_file:
                    f.write(f"map_Kd textures/{tex_file}\n")
                    if self.material_has_alpha.get(mat_name):
                        f.write(f"map_d textures/{tex_file}\n")
                else:
                    f.write(f"Kd {r:.4f} {g:.4f} {b:.4f}\n")

                f.write(f"Ka 0.1 0.1 0.1\n")
                f.write(f"Ks 0.0 0.0 0.0\n")
                f.write(f"d 1.0\n")
                f.write(f"illum 1\n\n")

        return mtl_path


def write_v28_color_map(output_path, v28_param0_colors):
    map_path = output_path.with_suffix('.v28_colors.txt')

    with open(map_path, 'w') as f:
        f.write("# V28 Param0 to Color Mapping\n")
        f.write("# Format: param0_id  R  G  B  hex_color\n")
        f.write("# These colors are deterministically generated from the param0 ID.\n")
        f.write("# Identify node types by loading the map in Minetest and comparing.\n\n")

        for param0 in sorted(v28_param0_colors.keys()):
            r, g, b = v28_param0_colors[param0]
            r_int = int(r * 255)
            g_int = int(g * 255)
            b_int = int(b * 255)
            hex_color = f"#{r_int:02x}{g_int:02x}{b_int:02x}"
            f.write(f"{param0:>6}  {r_int:>3} {g_int:>3} {b_int:>3}  {hex_color}\n")

    print(f"\nV28 color map written to {map_path}")
    return map_path

def main():
    parser = argparse.ArgumentParser(
        description='Export Minetest/Luanti map.sqlite to OBJ 3D model',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Coordinate examples:
  --min-node 0 0 0 --max-node 255 127 255               (near origin, 16 mapblocks)
  --min-node -16 -16 -16 --max-node 31 31 31            (small test area)

The script generates:
  <output>.obj          - 3D mesh file
  <output>.mtl          - Material definitions
  <output>.v28_colors.txt - V28 param0 color reference (if v28 blocks exist)
        """)

    parser.add_argument('--db', required=True, help='Path to map.sqlite database')
    parser.add_argument('--nodes', required=True, help='Path to nodes.lua from Minetest')
    parser.add_argument('--textures', action='append', default=[],
                       help='Path to game/mod root or textures dir. '
                            'Auto-discovers textures/ subdirs within. '
                            'Can be specified multiple times.')
    parser.add_argument('--output', default='map_export.obj',
                       help='Output OBJ file path (default: map_export.obj)')
    parser.add_argument('--min-node', nargs=3, type=int, default=[-993, -993, -993],
                       metavar=('X', 'Y', 'Z'),
                       help='Minimum node coordinates (default: -993 -993 -993)')
    parser.add_argument('--max-node', nargs=3, type=int, default=[1007, 1007, 1007],
                       metavar=('X', 'Y', 'Z'),
                       help='Maximum node coordinates (default: 1007 1007 1007)')

    args = parser.parse_args()

    min_node = tuple(args.min_node)
    max_node = tuple(args.max_node)
    min_mb = tuple(c // 16 for c in min_node)
    max_mb = tuple(c // 16 for c in max_node)

    print(f"Minetest Map Exporter")
    print(f"=" * 50)
    print(f"Database:       {args.db}")
    print(f"Node defs:      {args.nodes}")
    print(f"Output:         {args.output}")
    print(f"Node range:     {min_node} to {max_node}")
    print(f"Mapblock range: {min_mb} to {max_mb}")
    print(f"Mapblock count: ~{(max_mb[0]-min_mb[0]+1) * (max_mb[1]-min_mb[1]+1) * (max_mb[2]-min_mb[2]+1)}")

    print(f"\nLoading node definitions from {args.nodes}...")
    try:
        node_defs = parse_lua_table(args.nodes)
        print(f"  Loaded {len(node_defs)} node definitions")
    except Exception as e:
        print(f"  WARNING: Failed to parse nodes.lua: {e}", file=sys.stderr)
        print(f"  Continuing without node type information", file=sys.stderr)
        node_defs = {}

    texture_resolver = None
    tex_output_dir = None

    if args.textures:
        try:
            from texture_system import TextureResolver
            tex_dirs = []
            for path in args.textures:
                if os.path.isdir(path):
                    if os.path.basename(path) == 'textures':
                        tex_dirs.append(path)
                    else:
                        for root, dirs, files in os.walk(path):
                            if os.path.basename(root) == 'textures':
                                tex_dirs.append(root)
            if tex_dirs:
                texture_resolver = TextureResolver(tex_dirs)
                print(f"  Texture resolver: {len(tex_dirs)} dirs, "
                      f"{len(texture_resolver.path_index)} files indexed")

            output_path = Path(args.output)
            tex_output_dir = str(output_path.parent / 'textures')
            os.makedirs(tex_output_dir, exist_ok=True)
            print(f"  Texture output directory: {tex_output_dir}")
        except Exception as e:
            print(f"  WARNING: Failed to build texture resolver: {e}", file=sys.stderr)
            texture_resolver = None
    else:
        print(f"  No texture directories specified, using color-only materials")

    v28_param0_colors = {}
    exporter = MeshExporter(node_defs, v28_param0_colors, texture_resolver, tex_output_dir)

    print(f"\nQuerying blocks from database...")
    block_count = 0
    node_count = 0

    for bx, by, bz, block_data in query_blocks(args.db, min_mb, max_mb):
        exporter.add_block(bx, by, bz, block_data)
        block_count += 1
        node_count += len(block_data['nodes'])

        if block_count % 10000 == 0:
            print(f"  Processed {block_count} blocks, {node_count} nodes...", end='\r')

    print(f"\n\nLoaded {block_count} blocks containing {node_count} nodes")

    if node_count == 0:
        print("\nNo nodes found in the specified range. Nothing to export.")
        return

    exporter.generate_mesh(min_node, max_node)

    output_path = Path(args.output)
    obj_path = exporter.write_obj(output_path)
    mtl_path = exporter.write_mtl(output_path)

    if v28_param0_colors:
        write_v28_color_map(output_path, v28_param0_colors)

    print(f"\nDone! Files written:")
    print(f"  {obj_path}")
    print(f"  {mtl_path}")
    if v28_param0_colors:
        print(f"  {output_path.with_suffix('.v28_colors.txt')}")
    if exporter.material_textures:
        print(f"  textures/ ({len(exporter.material_textures)} textures resolved)")

if __name__ == '__main__':
    main()
