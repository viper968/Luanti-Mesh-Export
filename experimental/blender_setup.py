import bpy
import sys
import os
import json
import bmesh
import math
import numpy as np
from collections import deque

ISOLEVEL = 2.0
MAX_WATTS = 0.5
SOLID_FALLOFF_RADIUS = 2.0  # blocks — 0.5 = tight fit, 1.0 = default, 2.0 = pulled well clear of walls

script_dir = os.path.dirname(os.path.abspath(__file__))
obj_path = None
if "--" in sys.argv:
    idx = sys.argv.index("--")
    if idx + 1 < len(sys.argv):
        obj_path = sys.argv[idx + 1]

if os.path.exists(obj_path):
    try:
        bpy.ops.wm.obj_import(filepath=obj_path)
    except AttributeError:
        try:
            bpy.ops.import_scene.obj(filepath=obj_path)
        except AttributeError:
            print("ERROR: Enable io_scene_obj addon in Preferences > Add-ons.")
            sys.exit(1)
    print(f"Imported {obj_path}")
else:
    print(f"OBJ not found: {obj_path}")
    sys.exit(1)

mtl_path = os.path.join(script_dir, "testing.mtl")
light_permeable_names = set()
if os.path.exists(mtl_path):
    with open(mtl_path, "r") as mf:
        is_permeable = False
        for line in mf:
            line = line.strip()
            if line.startswith("# LIGHT_PERMEABLE"):
                is_permeable = True
            elif line.startswith("newmtl ") and is_permeable:
                light_permeable_names.add(line.split(None, 1)[1])
                is_permeable = False
            elif line.startswith("newmtl"):
                is_permeable = False

bpy.context.scene.render.engine = "CYCLES"
bpy.context.scene.cycles.transparent_max_bounces = 32
print("Render engine set to Cycles, transparent bounces = 32")

if light_permeable_names:
    print(f"Patching {len(light_permeable_names)} light-permeable materials for Cycles")
    for mat in bpy.data.materials:
        if mat.name in light_permeable_names:
            mat.use_nodes = True
            tree = mat.node_tree
            nodes = tree.nodes
            links = tree.links
            output = None
            principled = None
            for n in nodes:
                if n.type == "OUTPUT_MATERIAL":
                    output = n
                elif n.type == "BSDF_PRINCIPLED":
                    principled = n
            if output and principled:
                mat.use_transparent_shadow = True
                lp = nodes.new("ShaderNodeLightPath")
                transparent = nodes.new("ShaderNodeBsdfTransparent")
                transparent.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
                mix = nodes.new("ShaderNodeMixShader")
                lp.location = (principled.location.x + 200, principled.location.y + 250)
                transparent.location = (principled.location.x + 200, principled.location.y + 100)
                mix.location = (principled.location.x + 450, principled.location.y + 150)
                links.new(lp.outputs["Is Shadow Ray"], mix.inputs["Fac"])
                links.new(principled.outputs["BSDF"], mix.inputs[1])
                links.new(transparent.outputs["BSDF"], mix.inputs[2])
                for link in list(links):
                    if link.from_node == principled and link.to_node == output:
                        links.remove(link)
                links.new(mix.outputs["Shader"], output.inputs["Surface"])

lights_json_path = os.path.join(script_dir, "testing.lights.json")
if os.path.exists(lights_json_path):
    with open(lights_json_path, "r") as lf:
        lights_data = json.load(lf)
    sources = {tuple(s["pos"]): s["level"] for s in lights_data.get("sources", [])}
    solid_blocks = set(tuple(b) for b in lights_data.get("solid_blocks", []))
    bounds = lights_data.get("bounds", {})
    bx = bounds.get("x", [0, 0])
    by = bounds.get("y", [0, 0])
    bz = bounds.get("z", [0, 0])
    bound_tuple = ((bx[0], bx[1]), (by[0], by[1]), (bz[0], bz[1]))

    if sources:
        # Phase 1: BFS reachability mask
        print("Phase 1: BFS reachability...")
        reachable = set(sources.keys())
        queue = deque(sources.keys())
        bfs_count = 0
        while queue:
            x, y, z = queue.popleft()
            bfs_count += 1
            if bfs_count % 100 == 0:
                print(f"  BFS: {len(reachable)} cells reached, {len(queue)} in queue", end="\r")
            for dx, dy, dz in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
                nx, ny, nz = x+dx, y+dy, z+dz
                if (nx, ny, nz) in reachable:
                    continue
                if (nx, ny, nz) in solid_blocks:
                    continue
                if not (bx[0]<=nx<=bx[1] and by[0]<=ny<=by[1] and bz[0]<=nz<=bz[1]):
                    continue
                reachable.add((nx, ny, nz))
                queue.append((nx, ny, nz))
        print(f"\n  BFS complete: {len(reachable)} reachable cells")

        source_list = list(sources.items())

        # Phase 1b: Per-source BFS reachability
        # Phase 1 tells us which cells are reachable from anywhere.
        # This tells us which cells each specific source can reach without passing
        # through walls — fixing light bleeding through solid blocks into adjacent spaces.
        print("Phase 1b: Per-source reachability...")
        cells_i = np.array(list(reachable), dtype=np.int32)
        cell_to_idx = {tuple(pos): i for i, pos in enumerate(cells_i)}

        reach_mask = np.zeros((len(cell_to_idx), len(source_list)), dtype=bool)
        for s_idx, ((sx, sy, sz), level) in enumerate(source_list):
            src_reach = {(sx, sy, sz)}
            src_queue = deque([(sx, sy, sz, level)])
            while src_queue:
                cx, cy, cz, remaining = src_queue.popleft()
                if remaining <= 1:
                    continue
                for ddx, ddy, ddz in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
                    nb = (cx+ddx, cy+ddy, cz+ddz)
                    if nb in src_reach or nb in solid_blocks:
                        continue
                    if not (bx[0]<=nb[0]<=bx[1] and by[0]<=nb[1]<=by[1] and bz[0]<=nb[2]<=bz[1]):
                        continue
                    src_reach.add(nb)
                    src_queue.append((nb[0], nb[1], nb[2], remaining - 1))
            for pos in src_reach:
                if pos in cell_to_idx:
                    reach_mask[cell_to_idx[pos], s_idx] = True
            if s_idx % 10 == 0:
                print(f"  Per-source BFS: {s_idx+1}/{len(source_list)} sources", end="\r")
        print(f"\n  Per-source BFS complete")

        # Phase 2: Euclidean scalar field with solid boundary softening (numpy vectorized)
        print(f"Phase 2: Euclidean field ({len(reachable)} cells, {len(source_list)} sources)...")

        # Grid dimensions and offset computed here — also used by Phase 3 arr build
        (xmin,xmax),(ymin,ymax),(zmin,zmax) = bound_tuple
        shape  = (xmax-xmin+3, ymax-ymin+3, zmax-zmin+3)
        offset = (xmin-1, ymin-1, zmin-1)

        # Build 3D boolean solid grid for vectorized neighbour lookups
        solid_arr = np.zeros(shape, dtype=bool)
        for (x, y, z) in solid_blocks:
            ix, iy, iz = x - offset[0], y - offset[1], z - offset[2]
            if 0 <= ix < shape[0] and 0 <= iy < shape[1] and 0 <= iz < shape[2]:
                solid_arr[ix, iy, iz] = True

        # ── Light contribution: fully vectorized ──────────────────────────────
        # cells_i already built in Phase 1b
        cells_f    = cells_i.astype(np.float32)
        src_pos    = np.array([[x, y, z] for (x,y,z),_ in source_list], dtype=np.float32)  # (S, 3)
        src_levels = np.array([lv for _,lv in source_list], dtype=np.float32)               # (S,)

        # Process in chunks so peak memory stays bounded at CHUNK * S * 3 * 4 bytes
        CHUNK  = 50_000
        values = np.zeros(len(cells_f), dtype=np.float32)
        for start in range(0, len(cells_f), CHUNK):
            end   = min(start + CHUNK, len(cells_f))
            chunk = cells_f[start:end]                              # (C, 3)
            diff  = chunk[:, None, :] - src_pos[None, :, :]        # (C, S, 3)
            dists = np.sqrt(np.einsum('ijk,ijk->ij', diff, diff))   # (C, S)
            # reach_mask gates each source's contribution to only cells it can
            # physically reach — prevents light bleeding through solid walls
            contribs = np.maximum(0.0, src_levels[None, :] - dists)
            contribs *= reach_mask[start:end, :]
            values[start:end] = np.sum(contribs, axis=1)
            print(f"  Distance field: {end}/{len(cells_f)} cells", end="\r")
        print()

        # ── Solid boundary softening: vectorized over all 26 neighbours ───────
        lit_mask   = values > 0
        lit_i      = cells_i[lit_mask]         # (M, 3) integer positions of lit cells
        lit_values = values[lit_mask].copy()   # (M,)

        # box_sdf(cell, cell+offset) = sqrt(sum(max(0, |d_axis|-0.5)^2))
        # This value is CONSTANT per offset — precompute all 26 once.
        nb_offsets = np.array(
            [[ddx, ddy, ddz]
             for ddx in (-1, 0, 1)
             for ddy in (-1, 0, 1)
             for ddz in (-1, 0, 1)
             if not (ddx == 0 and ddy == 0 and ddz == 0)],
            dtype=np.int32)                                         # (26, 3)
        nb_bsdf = np.array([
            math.sqrt(max(0.0, abs(float(d[0])) - 0.5) ** 2
                    + max(0.0, abs(float(d[1])) - 0.5) ** 2
                    + max(0.0, abs(float(d[2])) - 0.5) ** 2)
            for d in nb_offsets], dtype=np.float32)                # (26,)

        # Convert lit positions to array-space indices for solid_arr lookups
        lit_arr_idx    = lit_i - np.array(offset, dtype=np.int32) # (M, 3)
        min_solid_dist = np.full(len(lit_i), np.inf, dtype=np.float32)

        for oi in range(len(nb_offsets)):
            bsdf = float(nb_bsdf[oi])
            if bsdf >= SOLID_FALLOFF_RADIUS:
                continue                                            # can't improve min, skip
            nb_idx = lit_arr_idx + nb_offsets[oi]                  # (M, 3)
            valid  = (
                (nb_idx[:, 0] >= 0) & (nb_idx[:, 0] < shape[0]) &
                (nb_idx[:, 1] >= 0) & (nb_idx[:, 1] < shape[1]) &
                (nb_idx[:, 2] >= 0) & (nb_idx[:, 2] < shape[2])
            )
            if not np.any(valid):
                continue
            is_solid_v = solid_arr[nb_idx[valid, 0], nb_idx[valid, 1], nb_idx[valid, 2]]
            solid_here = np.zeros(len(lit_i), dtype=bool)
            solid_here[valid] = is_solid_v
            min_solid_dist = np.where(solid_here & (bsdf < min_solid_dist),
                                      bsdf, min_solid_dist)

        # Linear ramp: 0 at solid face → 1 at SOLID_FALLOFF_RADIUS away
        ramp = np.where(min_solid_dist < SOLID_FALLOFF_RADIUS,
                        min_solid_dist / SOLID_FALLOFF_RADIUS, 1.0)
        lit_values *= ramp

        # Keep only positive cells; expose as numpy arrays for Phase 3 arr build
        pos_mask   = lit_values > 0
        field_pos  = lit_i[pos_mask]           # (F, 3) integer coords
        field_vals = lit_values[pos_mask]      # (F,) float32
        field = dict(zip(map(tuple, field_pos), field_vals.tolist()))
        print(f"  Field complete: {len(field)} cells with light")

        if field:
            # Phase 3: Marching cubes
            # shape and offset already computed in Phase 2; field_pos/field_vals are numpy arrays
            arr = np.zeros(shape, dtype=np.float32)
            arr[field_pos[:, 0] - offset[0],
                field_pos[:, 1] - offset[1],
                field_pos[:, 2] - offset[2]] = field_vals

            max_field_value = float(field_vals.max())
            isolevel = ISOLEVEL

            # Marching cubes edge table (256 entries)
            edge_table = [
                0x0, 0x109, 0x203, 0x30a, 0x80c, 0x905, 0xa0f, 0xb06,
                0x406, 0x50f, 0x605, 0x70c, 0xc0a, 0xd03, 0xe09, 0xf00,
                0x190, 0x99, 0x393, 0x29a, 0x99c, 0x895, 0xb9f, 0xa96,
                0x596, 0x49f, 0x795, 0x69c, 0xd9a, 0xc93, 0xf99, 0xe90,
                0x230, 0x339, 0x33, 0x13a, 0xa3c, 0xb35, 0x83f, 0x936,
                0x636, 0x73f, 0x435, 0x53c, 0xe3a, 0xf33, 0xc39, 0xd30,
                0x3a0, 0x2a9, 0x1a3, 0xaa, 0xbac, 0xaa5, 0x9af, 0x8a6,
                0x7a6, 0x6af, 0x5a5, 0x4ac, 0xfaa, 0xea3, 0xda9, 0xca0,
                0x8c0, 0x9c9, 0xac3, 0xbca, 0xcc, 0x1c5, 0x2cf, 0x3c6,
                0xcc6, 0xdcf, 0xec5, 0xfcc, 0x4ca, 0x5c3, 0x6c9, 0x7c0,
                0x950, 0x859, 0xb53, 0xa5a, 0x15c, 0x55, 0x35f, 0x256,
                0xd56, 0xc5f, 0xf55, 0xe5c, 0x55a, 0x453, 0x759, 0x650,
                0xaf0, 0xbf9, 0x8f3, 0x9fa, 0x2fc, 0x3f5, 0xff, 0x1f6,
                0xef6, 0xfff, 0xcf5, 0xdfc, 0x6fa, 0x7f3, 0x4f9, 0x5f0,
                0xb60, 0xa69, 0x963, 0x86a, 0x36c, 0x265, 0x16f, 0x66,
                0xf66, 0xe6f, 0xd65, 0xc6c, 0x76a, 0x663, 0x569, 0x460,
                0x460, 0x569, 0x663, 0x76a, 0xc6c, 0xd65, 0xe6f, 0xf66,
                0x66, 0x16f, 0x265, 0x36c, 0x86a, 0x963, 0xa69, 0xb60,
                0x5f0, 0x4f9, 0x7f3, 0x6fa, 0xdfc, 0xcf5, 0xfff, 0xef6,
                0x1f6, 0xff, 0x3f5, 0x2fc, 0x9fa, 0x8f3, 0xbf9, 0xaf0,
                0x650, 0x759, 0x453, 0x55a, 0xe5c, 0xf55, 0xc5f, 0xd56,
                0x256, 0x35f, 0x55, 0x15c, 0xa5a, 0xb53, 0x859, 0x950,
                0x7c0, 0x6c9, 0x5c3, 0x4ca, 0xfcc, 0xec5, 0xdcf, 0xcc6,
                0x3c6, 0x2cf, 0x1c5, 0xcc, 0xbca, 0xac3, 0x9c9, 0x8c0,
                0xca0, 0xda9, 0xea3, 0xfaa, 0x4ac, 0x5a5, 0x6af, 0x7a6,
                0x8a6, 0x9af, 0xaa5, 0xbac, 0xaa, 0x1a3, 0x2a9, 0x3a0,
                0xd30, 0xc39, 0xf33, 0xe3a, 0x53c, 0x435, 0x73f, 0x636,
                0x936, 0x83f, 0xb35, 0xa3c, 0x13a, 0x33, 0x339, 0x230,
                0xe90, 0xf99, 0xc93, 0xd9a, 0x69c, 0x795, 0x49f, 0x596,
                0xa96, 0xb9f, 0x895, 0x99c, 0x29a, 0x393, 0x99, 0x190,
                0xf00, 0xe09, 0xd03, 0xc0a, 0x70c, 0x605, 0x50f, 0x406,
                0xb06, 0xa0f, 0x905, 0x80c, 0x30a, 0x203, 0x109, 0x0]

            # Marching cubes triangle table (256 entries, -1 terminated)
            tri_table = [
                [ -1 ],
                [ 0, 3, 8, -1 ],
                [ 0, 9, 1, -1 ],
                [ 3, 8, 1, 1, 8, 9, -1 ],
                [ 2, 11, 3, -1 ],
                [ 8, 0, 11, 11, 0, 2, -1 ],
                [ 3, 2, 11, 1, 0, 9, -1 ],
                [ 11, 1, 2, 11, 9, 1, 11, 8, 9, -1 ],
                [ 1, 10, 2, -1 ],
                [ 0, 3, 8, 2, 1, 10, -1 ],
                [ 10, 2, 9, 9, 2, 0, -1 ],
                [ 8, 2, 3, 8, 10, 2, 8, 9, 10, -1 ],
                [ 11, 3, 10, 10, 3, 1, -1 ],
                [ 10, 0, 1, 10, 8, 0, 10, 11, 8, -1 ],
                [ 9, 3, 0, 9, 11, 3, 9, 10, 11, -1 ],
                [ 8, 9, 11, 11, 9, 10, -1 ],
                [ 4, 8, 7, -1 ],
                [ 7, 4, 3, 3, 4, 0, -1 ],
                [ 4, 8, 7, 0, 9, 1, -1 ],
                [ 1, 4, 9, 1, 7, 4, 1, 3, 7, -1 ],
                [ 8, 7, 4, 11, 3, 2, -1 ],
                [ 4, 11, 7, 4, 2, 11, 4, 0, 2, -1 ],
                [ 0, 9, 1, 8, 7, 4, 11, 3, 2, -1 ],
                [ 7, 4, 11, 11, 4, 2, 2, 4, 9, 2, 9, 1, -1 ],
                [ 4, 8, 7, 2, 1, 10, -1 ],
                [ 7, 4, 3, 3, 4, 0, 10, 2, 1, -1 ],
                [ 10, 2, 9, 9, 2, 0, 7, 4, 8, -1 ],
                [ 10, 2, 3, 10, 3, 4, 3, 7, 4, 9, 10, 4, -1 ],
                [ 1, 10, 3, 3, 10, 11, 4, 8, 7, -1 ],
                [ 10, 11, 1, 11, 7, 4, 1, 11, 4, 1, 4, 0, -1 ],
                [ 7, 4, 8, 9, 3, 0, 9, 11, 3, 9, 10, 11, -1 ],
                [ 7, 4, 11, 4, 9, 11, 9, 10, 11, -1 ],
                [ 9, 4, 5, -1 ],
                [ 9, 4, 5, 8, 0, 3, -1 ],
                [ 4, 5, 0, 0, 5, 1, -1 ],
                [ 5, 8, 4, 5, 3, 8, 5, 1, 3, -1 ],
                [ 9, 4, 5, 11, 3, 2, -1 ],
                [ 2, 11, 0, 0, 11, 8, 5, 9, 4, -1 ],
                [ 4, 5, 0, 0, 5, 1, 11, 3, 2, -1 ],
                [ 5, 1, 4, 1, 2, 11, 4, 1, 11, 4, 11, 8, -1 ],
                [ 1, 10, 2, 5, 9, 4, -1 ],
                [ 9, 4, 5, 0, 3, 8, 2, 1, 10, -1 ],
                [ 2, 5, 10, 2, 4, 5, 2, 0, 4, -1 ],
                [ 10, 2, 5, 5, 2, 4, 4, 2, 3, 4, 3, 8, -1 ],
                [ 11, 3, 10, 10, 3, 1, 4, 5, 9, -1 ],
                [ 4, 5, 9, 10, 0, 1, 10, 8, 0, 10, 11, 8, -1 ],
                [ 11, 3, 0, 11, 0, 5, 0, 4, 5, 10, 11, 5, -1 ],
                [ 4, 5, 8, 5, 10, 8, 10, 11, 8, -1 ],
                [ 8, 7, 9, 9, 7, 5, -1 ],
                [ 3, 9, 0, 3, 5, 9, 3, 7, 5, -1 ],
                [ 7, 0, 8, 7, 1, 0, 7, 5, 1, -1 ],
                [ 7, 5, 3, 3, 5, 1, -1 ],
                [ 5, 9, 7, 7, 9, 8, 2, 11, 3, -1 ],
                [ 2, 11, 7, 2, 7, 9, 7, 5, 9, 0, 2, 9, -1 ],
                [ 2, 11, 3, 7, 0, 8, 7, 1, 0, 7, 5, 1, -1 ],
                [ 2, 11, 1, 11, 7, 1, 7, 5, 1, -1 ],
                [ 8, 7, 9, 9, 7, 5, 2, 1, 10, -1 ],
                [ 10, 2, 1, 3, 9, 0, 3, 5, 9, 3, 7, 5, -1 ],
                [ 7, 5, 8, 5, 10, 2, 8, 5, 2, 8, 2, 0, -1 ],
                [ 10, 2, 5, 2, 3, 5, 3, 7, 5, -1 ],
                [ 8, 7, 5, 8, 5, 9, 11, 3, 10, 3, 1, 10, -1 ],
                [ 5, 11, 7, 10, 11, 5, 1, 9, 0, -1 ],
                [ 11, 5, 10, 7, 5, 11, 8, 3, 0, -1 ],
                [ 5, 11, 7, 10, 11, 5, -1 ],
                [ 6, 7, 11, -1 ],
                [ 7, 11, 6, 3, 8, 0, -1 ],
                [ 6, 7, 11, 0, 9, 1, -1 ],
                [ 9, 1, 8, 8, 1, 3, 6, 7, 11, -1 ],
                [ 3, 2, 7, 7, 2, 6, -1 ],
                [ 0, 7, 8, 0, 6, 7, 0, 2, 6, -1 ],
                [ 6, 7, 2, 2, 7, 3, 9, 1, 0, -1 ],
                [ 6, 7, 8, 6, 8, 1, 8, 9, 1, 2, 6, 1, -1 ],
                [ 11, 6, 7, 10, 2, 1, -1 ],
                [ 3, 8, 0, 11, 6, 7, 10, 2, 1, -1 ],
                [ 0, 9, 2, 2, 9, 10, 7, 11, 6, -1 ],
                [ 6, 7, 11, 8, 2, 3, 8, 10, 2, 8, 9, 10, -1 ],
                [ 7, 10, 6, 7, 1, 10, 7, 3, 1, -1 ],
                [ 8, 0, 7, 7, 0, 6, 6, 0, 1, 6, 1, 10, -1 ],
                [ 7, 3, 6, 3, 0, 9, 6, 3, 9, 6, 9, 10, -1 ],
                [ 6, 7, 10, 7, 8, 10, 8, 9, 10, -1 ],
                [ 11, 6, 8, 8, 6, 4, -1 ],
                [ 6, 3, 11, 6, 0, 3, 6, 4, 0, -1 ],
                [ 11, 6, 8, 8, 6, 4, 1, 0, 9, -1 ],
                [ 1, 3, 9, 3, 11, 6, 9, 3, 6, 9, 6, 4, -1 ],
                [ 2, 8, 3, 2, 4, 8, 2, 6, 4, -1 ],
                [ 4, 0, 6, 6, 0, 2, -1 ],
                [ 9, 1, 0, 2, 8, 3, 2, 4, 8, 2, 6, 4, -1 ],
                [ 9, 1, 4, 1, 2, 4, 2, 6, 4, -1 ],
                [ 4, 8, 6, 6, 8, 11, 1, 10, 2, -1 ],
                [ 1, 10, 2, 6, 3, 11, 6, 0, 3, 6, 4, 0, -1 ],
                [ 11, 6, 4, 11, 4, 8, 10, 2, 9, 2, 0, 9, -1 ],
                [ 10, 4, 9, 6, 4, 10, 11, 2, 3, -1 ],
                [ 4, 8, 3, 4, 3, 10, 3, 1, 10, 6, 4, 10, -1 ],
                [ 1, 10, 0, 10, 6, 0, 6, 4, 0, -1 ],
                [ 4, 10, 6, 9, 10, 4, 0, 8, 3, -1 ],
                [ 4, 10, 6, 9, 10, 4, -1 ],
                [ 6, 7, 11, 4, 5, 9, -1 ],
                [ 4, 5, 9, 7, 11, 6, 3, 8, 0, -1 ],
                [ 1, 0, 5, 5, 0, 4, 11, 6, 7, -1 ],
                [ 11, 6, 7, 5, 8, 4, 5, 3, 8, 5, 1, 3, -1 ],
                [ 3, 2, 7, 7, 2, 6, 9, 4, 5, -1 ],
                [ 5, 9, 4, 0, 7, 8, 0, 6, 7, 0, 2, 6, -1 ],
                [ 3, 2, 6, 3, 6, 7, 1, 0, 5, 0, 4, 5, -1 ],
                [ 6, 1, 2, 5, 1, 6, 4, 7, 8, -1 ],
                [ 10, 2, 1, 6, 7, 11, 4, 5, 9, -1 ],
                [ 0, 3, 8, 4, 5, 9, 11, 6, 7, 10, 2, 1, -1 ],
                [ 7, 11, 6, 2, 5, 10, 2, 4, 5, 2, 0, 4, -1 ],
                [ 8, 4, 7, 5, 10, 6, 3, 11, 2, -1 ],
                [ 9, 4, 5, 7, 10, 6, 7, 1, 10, 7, 3, 1, -1 ],
                [ 10, 6, 5, 7, 8, 4, 1, 9, 0, -1 ],
                [ 4, 3, 0, 7, 3, 4, 6, 5, 10, -1 ],
                [ 10, 6, 5, 8, 4, 7, -1 ],
                [ 9, 6, 5, 9, 11, 6, 9, 8, 11, -1 ],
                [ 11, 6, 3, 3, 6, 0, 0, 6, 5, 0, 5, 9, -1 ],
                [ 11, 6, 5, 11, 5, 0, 5, 1, 0, 8, 11, 0, -1 ],
                [ 11, 6, 3, 6, 5, 3, 5, 1, 3, -1 ],
                [ 9, 8, 5, 8, 3, 2, 5, 8, 2, 5, 2, 6, -1 ],
                [ 5, 9, 6, 9, 0, 6, 0, 2, 6, -1 ],
                [ 1, 6, 5, 2, 6, 1, 3, 0, 8, -1 ],
                [ 1, 6, 5, 2, 6, 1, -1 ],
                [ 2, 1, 10, 9, 6, 5, 9, 11, 6, 9, 8, 11, -1 ],
                [ 9, 0, 1, 3, 11, 2, 5, 10, 6, -1 ],
                [ 11, 0, 8, 2, 0, 11, 10, 6, 5, -1 ],
                [ 3, 11, 2, 5, 10, 6, -1 ],
                [ 1, 8, 3, 9, 8, 1, 5, 10, 6, -1 ],
                [ 6, 5, 10, 0, 1, 9, -1 ],
                [ 8, 3, 0, 5, 10, 6, -1 ],
                [ 6, 5, 10, -1 ],
                [ 10, 5, 6, -1 ],
                [ 0, 3, 8, 6, 10, 5, -1 ],
                [ 10, 5, 6, 9, 1, 0, -1 ],
                [ 3, 8, 1, 1, 8, 9, 6, 10, 5, -1 ],
                [ 2, 11, 3, 6, 10, 5, -1 ],
                [ 8, 0, 11, 11, 0, 2, 5, 6, 10, -1 ],
                [ 1, 0, 9, 2, 11, 3, 6, 10, 5, -1 ],
                [ 5, 6, 10, 11, 1, 2, 11, 9, 1, 11, 8, 9, -1 ],
                [ 5, 6, 1, 1, 6, 2, -1 ],
                [ 5, 6, 1, 1, 6, 2, 8, 0, 3, -1 ],
                [ 6, 9, 5, 6, 0, 9, 6, 2, 0, -1 ],
                [ 6, 2, 5, 2, 3, 8, 5, 2, 8, 5, 8, 9, -1 ],
                [ 3, 6, 11, 3, 5, 6, 3, 1, 5, -1 ],
                [ 8, 0, 1, 8, 1, 6, 1, 5, 6, 11, 8, 6, -1 ],
                [ 11, 3, 6, 6, 3, 5, 5, 3, 0, 5, 0, 9, -1 ],
                [ 5, 6, 9, 6, 11, 9, 11, 8, 9, -1 ],
                [ 5, 6, 10, 7, 4, 8, -1 ],
                [ 0, 3, 4, 4, 3, 7, 10, 5, 6, -1 ],
                [ 5, 6, 10, 4, 8, 7, 0, 9, 1, -1 ],
                [ 6, 10, 5, 1, 4, 9, 1, 7, 4, 1, 3, 7, -1 ],
                [ 7, 4, 8, 6, 10, 5, 2, 11, 3, -1 ],
                [ 10, 5, 6, 4, 11, 7, 4, 2, 11, 4, 0, 2, -1 ],
                [ 4, 8, 7, 6, 10, 5, 3, 2, 11, 1, 0, 9, -1 ],
                [ 1, 2, 10, 11, 7, 6, 9, 5, 4, -1 ],
                [ 2, 1, 6, 6, 1, 5, 8, 7, 4, -1 ],
                [ 0, 3, 7, 0, 7, 4, 2, 1, 6, 1, 5, 6, -1 ],
                [ 8, 7, 4, 6, 9, 5, 6, 0, 9, 6, 2, 0, -1 ],
                [ 7, 2, 3, 6, 2, 7, 5, 4, 9, -1 ],
                [ 4, 8, 7, 3, 6, 11, 3, 5, 6, 3, 1, 5, -1 ],
                [ 5, 0, 1, 4, 0, 5, 7, 6, 11, -1 ],
                [ 9, 5, 4, 6, 11, 7, 0, 8, 3, -1 ],
                [ 11, 7, 6, 9, 5, 4, -1 ],
                [ 6, 10, 4, 4, 10, 9, -1 ],
                [ 6, 10, 4, 4, 10, 9, 3, 8, 0, -1 ],
                [ 0, 10, 1, 0, 6, 10, 0, 4, 6, -1 ],
                [ 6, 10, 1, 6, 1, 8, 1, 3, 8, 4, 6, 8, -1 ],
                [ 9, 4, 10, 10, 4, 6, 3, 2, 11, -1 ],
                [ 2, 11, 8, 2, 8, 0, 6, 10, 4, 10, 9, 4, -1 ],
                [ 11, 3, 2, 0, 10, 1, 0, 6, 10, 0, 4, 6, -1 ],
                [ 6, 8, 4, 11, 8, 6, 2, 10, 1, -1 ],
                [ 4, 1, 9, 4, 2, 1, 4, 6, 2, -1 ],
                [ 3, 8, 0, 4, 1, 9, 4, 2, 1, 4, 6, 2, -1 ],
                [ 6, 2, 4, 4, 2, 0, -1 ],
                [ 3, 8, 2, 8, 4, 2, 4, 6, 2, -1 ],
                [ 4, 6, 9, 6, 11, 3, 9, 6, 3, 9, 3, 1, -1 ],
                [ 8, 6, 11, 4, 6, 8, 9, 0, 1, -1 ],
                [ 11, 3, 6, 3, 0, 6, 0, 4, 6, -1 ],
                [ 8, 6, 11, 4, 6, 8, -1 ],
                [ 10, 7, 6, 10, 8, 7, 10, 9, 8, -1 ],
                [ 3, 7, 0, 7, 6, 10, 0, 7, 10, 0, 10, 9, -1 ],
                [ 6, 10, 7, 7, 10, 8, 8, 10, 1, 8, 1, 0, -1 ],
                [ 6, 10, 7, 10, 1, 7, 1, 3, 7, -1 ],
                [ 3, 2, 11, 10, 7, 6, 10, 8, 7, 10, 9, 8, -1 ],
                [ 2, 9, 0, 10, 9, 2, 6, 11, 7, -1 ],
                [ 0, 8, 3, 7, 6, 11, 1, 2, 10, -1 ],
                [ 7, 6, 11, 1, 2, 10, -1 ],
                [ 2, 1, 9, 2, 9, 7, 9, 8, 7, 6, 2, 7, -1 ],
                [ 2, 7, 6, 3, 7, 2, 0, 1, 9, -1 ],
                [ 8, 7, 0, 7, 6, 0, 6, 2, 0, -1 ],
                [ 7, 2, 3, 6, 2, 7, -1 ],
                [ 8, 1, 9, 3, 1, 8, 11, 7, 6, -1 ],
                [ 11, 7, 6, 1, 9, 0, -1 ],
                [ 6, 11, 7, 0, 8, 3, -1 ],
                [ 11, 7, 6, -1 ],
                [ 7, 11, 5, 5, 11, 10, -1 ],
                [ 10, 5, 11, 11, 5, 7, 0, 3, 8, -1 ],
                [ 7, 11, 5, 5, 11, 10, 0, 9, 1, -1 ],
                [ 7, 11, 10, 7, 10, 5, 3, 8, 1, 8, 9, 1, -1 ],
                [ 5, 2, 10, 5, 3, 2, 5, 7, 3, -1 ],
                [ 5, 7, 10, 7, 8, 0, 10, 7, 0, 10, 0, 2, -1 ],
                [ 0, 9, 1, 5, 2, 10, 5, 3, 2, 5, 7, 3, -1 ],
                [ 9, 7, 8, 5, 7, 9, 10, 1, 2, -1 ],
                [ 1, 11, 2, 1, 7, 11, 1, 5, 7, -1 ],
                [ 8, 0, 3, 1, 11, 2, 1, 7, 11, 1, 5, 7, -1 ],
                [ 7, 11, 2, 7, 2, 9, 2, 0, 9, 5, 7, 9, -1 ],
                [ 7, 9, 5, 8, 9, 7, 3, 11, 2, -1 ],
                [ 3, 1, 7, 7, 1, 5, -1 ],
                [ 8, 0, 7, 0, 1, 7, 1, 5, 7, -1 ],
                [ 0, 9, 3, 9, 5, 3, 5, 7, 3, -1 ],
                [ 9, 7, 8, 5, 7, 9, -1 ],
                [ 8, 5, 4, 8, 10, 5, 8, 11, 10, -1 ],
                [ 0, 3, 11, 0, 11, 5, 11, 10, 5, 4, 0, 5, -1 ],
                [ 1, 0, 9, 8, 5, 4, 8, 10, 5, 8, 11, 10, -1 ],
                [ 10, 3, 11, 1, 3, 10, 9, 5, 4, -1 ],
                [ 3, 2, 8, 8, 2, 4, 4, 2, 10, 4, 10, 5, -1 ],
                [ 10, 5, 2, 5, 4, 2, 4, 0, 2, -1 ],
                [ 5, 4, 9, 8, 3, 0, 10, 1, 2, -1 ],
                [ 2, 10, 1, 4, 9, 5, -1 ],
                [ 8, 11, 4, 11, 2, 1, 4, 11, 1, 4, 1, 5, -1 ],
                [ 0, 5, 4, 1, 5, 0, 2, 3, 11, -1 ],
                [ 0, 11, 2, 8, 11, 0, 4, 9, 5, -1 ],
                [ 5, 4, 9, 2, 3, 11, -1 ],
                [ 4, 8, 5, 8, 3, 5, 3, 1, 5, -1 ],
                [ 0, 5, 4, 1, 5, 0, -1 ],
                [ 5, 4, 9, 3, 0, 8, -1 ],
                [ 5, 4, 9, -1 ],
                [ 11, 4, 7, 11, 9, 4, 11, 10, 9, -1 ],
                [ 0, 3, 8, 11, 4, 7, 11, 9, 4, 11, 10, 9, -1 ],
                [ 11, 10, 7, 10, 1, 0, 7, 10, 0, 7, 0, 4, -1 ],
                [ 3, 10, 1, 11, 10, 3, 7, 8, 4, -1 ],
                [ 3, 2, 10, 3, 10, 4, 10, 9, 4, 7, 3, 4, -1 ],
                [ 9, 2, 10, 0, 2, 9, 8, 4, 7, -1 ],
                [ 3, 4, 7, 0, 4, 3, 1, 2, 10, -1 ],
                [ 7, 8, 4, 10, 1, 2, -1 ],
                [ 7, 11, 4, 4, 11, 9, 9, 11, 2, 9, 2, 1, -1 ],
                [ 1, 9, 0, 4, 7, 8, 2, 3, 11, -1 ],
                [ 7, 11, 4, 11, 2, 4, 2, 0, 4, -1 ],
                [ 4, 7, 8, 2, 3, 11, -1 ],
                [ 9, 4, 1, 4, 7, 1, 7, 3, 1, -1 ],
                [ 7, 8, 4, 1, 9, 0, -1 ],
                [ 3, 4, 7, 0, 4, 3, -1 ],
                [ 7, 8, 4, -1 ],
                [ 11, 10, 8, 8, 10, 9, -1 ],
                [ 0, 3, 9, 3, 11, 9, 11, 10, 9, -1 ],
                [ 1, 0, 10, 0, 8, 10, 8, 11, 10, -1 ],
                [ 10, 3, 11, 1, 3, 10, -1 ],
                [ 3, 2, 8, 2, 10, 8, 10, 9, 8, -1 ],
                [ 9, 2, 10, 0, 2, 9, -1 ],
                [ 8, 3, 0, 10, 1, 2, -1 ],
                [ 2, 10, 1, -1 ],
                [ 2, 1, 11, 1, 9, 11, 9, 8, 11, -1 ],
                [ 11, 2, 3, 9, 0, 1, -1 ],
                [ 11, 0, 8, 2, 0, 11, -1 ],
                [ 3, 11, 2, -1 ],
                [ 1, 8, 3, 9, 8, 1, -1 ],
                [ 1, 9, 0, -1 ],
                [ 8, 3, 0, -1 ],
                [ -1 ],
            ]

            # Phase 3: Marching cubes (vectorized cubeindex, loop only over active boundary cubes)

            # ── Vectorized cubeindex computation ─────────────────────────────
            # Vertex bit mapping: bit n set means corner n > isolevel.
            # Corner n encodes offsets via bits: bit0=+i, bit1=+j, bit2=+k
            c = [
                arr[:-1, :-1, :-1],  # v0: (i,   j,   k  )
                arr[1:,  :-1, :-1],  # v1: (i+1, j,   k  )
                arr[:-1, 1:,  :-1],  # v2: (i,   j+1, k  )
                arr[1:,  1:,  :-1],  # v3: (i+1, j+1, k  )
                arr[:-1, :-1, 1: ],  # v4: (i,   j,   k+1)
                arr[1:,  :-1, 1: ],  # v5: (i+1, j,   k+1)
                arr[:-1, 1:,  1: ],  # v6: (i,   j+1, k+1)
                arr[1:,  1:,  1: ],  # v7: (i+1, j+1, k+1)
            ]
            cubeindex_arr = np.zeros(c[0].shape, dtype=np.int32)
            for ci in range(8):
                cubeindex_arr |= (c[ci] > isolevel).astype(np.int32) << ci

            # Find only cubes that cross the isosurface (edge_table != 0)
            edge_table_np = np.array(edge_table, dtype=np.int32)
            active_ijk    = np.argwhere(edge_table_np[cubeindex_arr] != 0)  # (A, 3)

            # Pre-extract all 8 corner values for every active cube at once
            ai, aj, ak = active_ijk[:, 0], active_ijk[:, 1], active_ijk[:, 2]
            corner_vals = np.stack([
                arr[ai,   aj,   ak  ],
                arr[ai+1, aj,   ak  ],
                arr[ai,   aj+1, ak  ],
                arr[ai+1, aj+1, ak  ],
                arr[ai,   aj,   ak+1],
                arr[ai+1, aj,   ak+1],
                arr[ai,   aj+1, ak+1],
                arr[ai+1, aj+1, ak+1],
            ], axis=1)  # (A, 8)

            edge_pairs = [
                (0,1),(1,3),(3,2),(2,0),
                (4,5),(5,7),(7,6),(6,4),
                (0,4),(1,5),(3,7),(2,6),
            ]

            all_verts = []
            all_tris  = []
            total_active = len(active_ijk)
            print(f"Phase 3: Marching cubes ({total_active} active / {cubeindex_arr.size} total cubes)...")

            for idx_num in range(total_active):
                if idx_num % 5000 == 0:
                    pct = idx_num * 100 // max(total_active, 1)
                    print(f"  Marching cubes: {pct}% ({len(all_verts)} verts)", end="\r")

                i, j, k  = int(active_ijk[idx_num, 0]), int(active_ijk[idx_num, 1]), int(active_ijk[idx_num, 2])
                cube      = corner_vals[idx_num]       # (8,) pre-extracted
                cubeindex = int(cubeindex_arr[i, j, k])
                edges     = edge_table[cubeindex]
                tri_list  = tri_table[cubeindex]
                if tri_list[0] == -1:
                    continue

                edge_verts = [None] * 12
                for ei in range(12):
                    if edges & (1 << ei):
                        a, b   = edge_pairs[ei]
                        va, vb = float(cube[a]), float(cube[b])
                        t      = (isolevel - va) / (vb - va) if abs(vb - va) > 1e-10 else 0.5
                        vx = float(i + (a & 1))       + t * float((b & 1)       - (a & 1))
                        vy = float(j + ((a>>1) & 1))  + t * float(((b>>1) & 1)  - ((a>>1) & 1))
                        vz = float(k + ((a>>2) & 1))  + t * float(((b>>2) & 1)  - ((a>>2) & 1))
                        edge_verts[ei] = (vx, vy, vz)

                for tidx in range(0, len(tri_list), 3):
                    if tidx + 2 >= len(tri_list):
                        break
                    e0, e1, e2 = tri_list[tidx], tri_list[tidx+1], tri_list[tidx+2]
                    if edge_verts[e0] is None or edge_verts[e1] is None or edge_verts[e2] is None:
                        continue
                    base = len(all_verts)
                    all_verts.append(edge_verts[e0])
                    all_verts.append(edge_verts[e1])
                    all_verts.append(edge_verts[e2])
                    all_tris.append((base, base + 1, base + 2))

            if all_verts:
                mesh = bpy.data.meshes.new("LightVolume")
                obj = bpy.data.objects.new("LightVolume", mesh)
                bpy.context.scene.collection.objects.link(obj)
                # Luanti→Blender axis mapping: bx=lx, by=-lz, bz=ly
                # Object origin placed at Blender equivalent of Luanti corner (xmin, ymin, zmin):
                #   bx=xmin, by=-zmin, bz=ymin
                obj.location = (xmin, -zmin, ymin)
                # Invisible to camera rays so it never renders as a bright blob,
                # but diffuse/glossy rays still pick up the emission for scene lighting
                obj.visible_camera = False
                obj.visible_shadow = False

                bm = bmesh.new()
                col_layer = bm.loops.layers.color.new("light_level")

                # Array-space vertex v → Luanti: lx=v[0]-1+xmin, ly=v[1]-1+ymin, lz=v[2]-1+zmin
                # Subtract object origin and apply axis mapping:
                #   bx = lx - xmin        = v[0]-1
                #   by = -lz - (-zmin)    = -(v[2]-1)
                #   bz = ly - ymin        = v[1]-1
                bverts = [bm.verts.new((v[0] - 1, -(v[2] - 1), v[1] - 1)) for v in all_verts]
                for tri_idx in all_tris:
                    try:
                        face = bm.faces.new([bverts[i] for i in tri_idx])
                    except ValueError:
                        continue
                    # All isosurface vertices sit exactly at isolevel by definition,
                    # so emit constant full-white — Multiply node scales to MAX_WATTS
                    for loop in face.loops:
                        loop[col_layer] = (1.0, 1.0, 1.0, 1.0)

                bm.to_mesh(mesh)
                bm.free()

                mat = bpy.data.materials.new(name="LightVolumeEmission")
                mat.use_nodes = True
                tree = mat.node_tree
                nodes = tree.nodes
                links = tree.links
                nodes.clear()
                vc = nodes.new("ShaderNodeVertexColor")
                vc.layer_name = "light_level"
                sep = nodes.new("ShaderNodeSeparateColor")
                multiply = nodes.new("ShaderNodeMath")
                multiply.operation = "MULTIPLY"
                multiply.inputs[1].default_value = MAX_WATTS
                emission = nodes.new("ShaderNodeEmission")
                emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
                output = nodes.new("ShaderNodeOutputMaterial")
                links.new(vc.outputs["Color"], sep.inputs["Color"])
                links.new(sep.outputs["Red"], multiply.inputs[0])
                links.new(multiply.outputs[0], emission.inputs["Strength"])
                links.new(emission.outputs[0], output.inputs["Surface"])
                if obj.data.materials:
                    obj.data.materials[0] = mat
                else:
                    obj.data.materials.append(mat)
                print(f"Created LightVolume: {len(all_verts)} vertices, {len(all_tris)} triangles")
            else:
                print("Marching cubes produced no geometry")
        else:
            print("No field values above zero")
else:
    print(f"Lights JSON not found: {lights_json_path}")

print("Setup complete!")
