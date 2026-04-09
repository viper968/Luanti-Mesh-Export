import bpy
import sys
import os
import json
import bmesh
import math
import numpy as np
from collections import deque

ISOLEVEL = 2.0
MAX_WATTS = 100.0
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

        # Phase 2: Euclidean scalar field with solid boundary softening
        source_list = list(sources.items())
        print(f"Phase 2: Euclidean field ({len(reachable)} cells, {len(source_list)} sources)...")

        def box_sdf(px, py, pz, sx, sy, sz):
            """Distance from cell centre (px,py,pz) to the face of a unit solid block at (sx,sy,sz)."""
            dx = max(0.0, abs(px - sx) - 0.5)
            dy = max(0.0, abs(py - sy) - 0.5)
            dz = max(0.0, abs(pz - sz) - 0.5)
            return math.sqrt(dx*dx + dy*dy + dz*dz)

        field = {}
        fi = 0
        for pos in reachable:
            fi += 1
            if fi % 100 == 0:
                print(f"  Field: {fi}/{len(reachable)} cells done", end="\r")
            px, py, pz = pos

            # Light contribution summed from all sources
            value = 0.0
            for (sx, sy, sz), level in source_list:
                dist = math.sqrt((px-sx)**2 + (py-sy)**2 + (pz-sz)**2)
                contrib = level - dist
                if contrib > 0:
                    value += contrib

            if value <= 0:
                continue

            # Solid boundary softening: check all 26 immediate neighbours for solid blocks.
            # Only neighbours within 1 cell radius can cause clipping so no wider search needed.
            min_solid_dist = float('inf')
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    for ddz in (-1, 0, 1):
                        if ddx == 0 and ddy == 0 and ddz == 0:
                            continue
                        nb = (px + ddx, py + ddy, pz + ddz)
                        if nb in solid_blocks:
                            d = box_sdf(px, py, pz, nb[0], nb[1], nb[2])
                            if d < min_solid_dist:
                                min_solid_dist = d

            # Linear ramp: 0 at solid face → 1 at SOLID_FALLOFF_RADIUS blocks away
            if min_solid_dist < SOLID_FALLOFF_RADIUS:
                value *= min_solid_dist / SOLID_FALLOFF_RADIUS

            if value > 0:
                field[pos] = value
        print(f"\n  Field complete: {len(field)} cells with light")

        if field:
            # Phase 3: Marching cubes
            (xmin,xmax),(ymin,ymax),(zmin,zmax) = bound_tuple
            shape = (xmax-xmin+3, ymax-ymin+3, zmax-zmin+3)
            arr = np.zeros(shape, dtype=np.float32)
            offset = (xmin-1, ymin-1, zmin-1)
            for (x,y,z), v in field.items():
                ix = x - offset[0]
                iy = y - offset[1]
                iz = z - offset[2]
                arr[ix, iy, iz] = v

            max_field_value = max(field.values())
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

            # Marching cubes interpolation
            all_verts = []
            all_tris = []
            all_tri_vals = []
            nx, ny, nz = arr.shape
            mc_total = (nx - 1) * (ny - 1) * (nz - 1)
            mc_count = 0
            print(f"Phase 3: Marching cubes ({mc_total} cubes to scan)...")
            for i in range(nx - 1):
                for j in range(ny - 1):
                    for k in range(nz - 1):
                        mc_count += 1
                        if mc_count % 100 == 0:
                            pct = mc_count * 100 // mc_total
                            print(f"  Marching cubes: {pct}% ({len(all_verts)} verts, {len(all_tris)} tris)", end="\r")
                        cube = [
                            arr[i, j, k], arr[i+1, j, k], arr[i, j+1, k], arr[i+1, j+1, k],
                            arr[i, j, k+1], arr[i+1, j, k+1], arr[i, j+1, k+1], arr[i+1, j+1, k+1],
                        ]
                        cubeindex = 0
                        for ci in range(8):
                            if cube[ci] > isolevel:
                                cubeindex |= (1 << ci)
                        edges = edge_table[cubeindex]
                        if edges == 0:
                            continue
                        tri_list = tri_table[cubeindex]
                        if tri_list[0] == -1:
                            continue

                        # Edge vertex interpolation
                        edge_verts = [None] * 12
                        edge_vals = [0.0] * 12
                        # Edge pairs match the gist convention:
                        # 0:(0,1) 1:(1,3) 2:(3,2) 3:(2,0)
                        # 4:(4,5) 5:(5,7) 6:(7,6) 7:(6,4)
                        # 8:(0,4) 9:(1,5) 10:(3,7) 11:(2,6)
                        edge_pairs = [
                            (0,1),(1,3),(3,2),(2,0),
                            (4,5),(5,7),(7,6),(6,4),
                            (0,4),(1,5),(3,7),(2,6),
                        ]
                        for ei in range(12):
                            if edges & (1 << ei):
                                a, b = edge_pairs[ei]
                                va, vb = cube[a], cube[b]
                                if abs(vb - va) > 1e-10:
                                    t = (isolevel - va) / (vb - va)
                                else:
                                    t = 0.5
                                pa = (float(i + (a & 1)), float(j + ((a >> 1) & 1)), float(k + ((a >> 2) & 1)))
                                pb = (float(i + (b & 1)), float(j + ((b >> 1) & 1)), float(k + ((b >> 2) & 1)))
                                vx = pa[0] + t * (pb[0] - pa[0])
                                vy = pa[1] + t * (pb[1] - pa[1])
                                vz = pa[2] + t * (pb[2] - pa[2])
                                edge_verts[ei] = (vx, vy, vz)
                                edge_vals[ei] = va + t * (vb - va)

                        for idx in range(0, len(tri_list), 3):
                            if idx + 2 >= len(tri_list):
                                break
                            e0, e1, e2 = tri_list[idx], tri_list[idx+1], tri_list[idx+2]
                            if edge_verts[e0] is None or edge_verts[e1] is None or edge_verts[e2] is None:
                                continue
                            all_verts.append(edge_verts[e0])
                            all_verts.append(edge_verts[e1])
                            all_verts.append(edge_verts[e2])
                            all_tris.append((len(all_verts)-3, len(all_verts)-2, len(all_verts)-1))
                            all_tri_vals.append((edge_vals[e0], edge_vals[e1], edge_vals[e2]))

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
