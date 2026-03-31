# Luanti-Mesh-Export

if any issues are found with exports please report it with what is seen, how its incorrect, what game is used, what mod the node comes from, this helps me quickly setup a world with said node to be able to easily debug what the node shows and how it gets processed, if said information isn't provided it will be difficult to debug whats wrong and how the block is pocessed

### `export_map.py`
Main exporter that converts Luanti `map.sqlite` world data to OBJ+MTL format for import into 3D software (Blender, etc.).

### `node_def mod`
Luanti mod that registers a `/dumpnodes` chat command. Dumps all registered node definitions to `<worldpath>/nodes.lua` as a Lua table. Works with mod security enabled.

### `map_block.py`
Standalone debug tool for inspecting individual MapBlock binary blobs. Supports v28 (zlib) and v29 (zstd) serialization. Can export block data to JSON.

### `texture_system.py`
Library for parsing Minetest texture modifier strings and resolving them to PIL images. Used by `export_map.py` for texture processing.

---

## Supported Drawtypes

| Drawtype                | Status  | Rotation | Per-face Textures | Notes                                                 |
|-------------------------|---------|----------|-------------------|-------------------------------------------------------|
| `normal`                | Full    | N/A      | Yes               | Standard full blocks                                  |
| `nodebox` (fixed)       | Full    | Facedir  | Yes               | Custom box shapes with full facedir (24 orientations) |
| `nodebox` (leveled)     | Partial | Facedir  | Yes               | Height from param2, but no liquid-style leveling      |
| `nodebox` (wallmounted) | Partial | Limited  | Yes               | Basic box rotation only                               |
| `nodebox` (connected)   | Partial | No       | Yes               | Fixed box only, no connect/disconnect logic           |
| `mesh`                  | Full    | Facedir  | Yes               | Loads .obj files from mod `models/` directories       |
| `plantlike`             | Partial | Facedir  | No                | Cross-plane rendering, no visual_scale                |
| `plantlike_rooted`      | Partial | Facedir  | No                | Same as plantlike                                     |
| `torchlike`             | Partial | Limited  | No                | Basic cross-planes, param2 placement only             |
| `signlike`              | Partial | No       | No                | Single flat plane                                     |
| `firelike`              | Partial | No       | No                | Cross-planes                                          |
| `liquid`                | Full    | N/A      | Yes               | Full cube rendering                                   |
| `flowingliquid`         | Full    | N/A      | Yes               | Full cube rendering                                   |
| `glasslike`             | Full    | N/A      | Yes               | Always shows all faces                                |
| `allfaces`              | Full    | N/A      | Yes               | Always shows all faces                                |
| `glasslike_framed`      | Full    | N/A      | Yes               | Always shows all faces                                |
| `airlike`               | N/A     | N/A      | N/A               | Skipped (no geometry)                                 |

---

## Facedir Rotation System

Full 24-orientation support for `paramtype2 = "facedir"`:

- **facedir / 4** = axis direction (0=Y+, 1=Z+, 2=Z-, 3=X+, 4=X-, 5=Y-)
- **facedir % 4** = left-handed spin around that axis (0-3 quarter turns)
- Applied in order: spin first, then axis rotation
- Rotation center: node center (0.5, 0.5, 0.5) for nodeboxes, mesh origin for mesh nodes

### Tile Index Remapping
Uses `FACEDIR_TO_TILE_INDICES` table (from Meshport) to remap which tile is applied to each face based on facedir value. This ensures textures rotate with the node orientation.

### UV Rotation
Uses `FACEDIR_TO_TILE_ROTATIONS` table (from Meshport) to apply per-face UV quarter-turns (0-3) based on facedir value. Rotates UV coordinates around the face center.

---

## Texture System Capabilities

### Supported Texture Modifiers
- `^[colorize:<color>[:<ratio>]` - Color overlay
- `^[multiply:<color>` - Color multiplication
- `^[opacity:<ratio>` - Alpha modification
- `^[mask:<texture>` - Bitwise masking
- `^[transform<R90|R180|R270|FX|FY>` - Geometric transforms
- `^[invert:<mode>` - Channel inversion
- `^[brighten` - Brightness boost
- `^[noalpha` - Force opaque
- `^[makealpha:<r>,<g>,<b>` - Color-key transparency
- `^[lowpart:<percent>:<texture>` - Partial overlay
- `^[crack[:<opacity>]:[<framecount>:]<tilecount>:<frame>` - Crack overlay
- `^[sheet:<w>x<h>:<x>,<y>` - Tile sheet extraction
- `^[combine:<w>x<h>:<textures>` - Texture composition
- `^[verticalframe:<framecount>:<frame>` - Animation frame
- `^[png:<data>` - Embedded PNG
- `^[inventorycube{<top>{<left>{<right>` - Inventory cube generation

### Texture Properties
- `backface_culling` - Per-tile backface visibility
- `align_style` - "node", "world", or "user" alignment
- `scale` - Texture scaling factor
- `animation` - Animated tile support (vertical frames)
- `name` / `image` - Texture file reference

### Transparency Handling
- Detects RGBA images with alpha channels
- Sets `d` (transparency) flag in MTL when alpha min < 255
- Supports `use_texture_alpha = "clip"` and `"blend"` modes

---

## Map Data Handling

### Supported Serialization Versions
- **v28 (0x1C)**: zlib-compressed, legacy format
- **v29 (0x1D)**: zstd-compressed, post-5.12 format

### Coordinate System
- Minetest: Y-up, Z+ = south, node positions are integer coordinates (SWB corner)
- OBJ export: Same orientation (Y-up, Z+ = south)
- Node content: 0-1 range relative to node position

### Mapblock Format
- 16x16x16 nodes per block
- Content ID mapping (node name ↔ numeric ID)
- param0 = content ID, param1 = light/other data, param2 = facedir/wallmounted/etc.

---

## Known Limitations

### Face Culling
- Only culls faces between full blocks (normal, liquid, flowingliquid)
- Partial blocks (nodebox, mesh, plantlike) always show all faces
- No neighbor-aware culling for connected nodeboxes

### Texture Orientation
- UV rotation only applied to nodeboxes with facedir
- Mesh nodes use UV coordinates from .obj files (no facedir UV rotation)
- Plantlike/torchlike/signlike use fixed UV mapping

### Missing Drawtypes
- `raillike` - Not implemented
- `fencelike` - Not implemented
- `nodebox` (connected) - Only fixed box shown, no connect/disconnect geometry
- `mesh` with `connected_nodebox` - Not handled

### Mesh Handling
- Only loads .obj files (no .b3d support)
- Mesh must be centered at origin for correct rotation
- No support for mesh groups with different materials per group face
- `visual_scale` applied but no mesh offset support

### Node Definition Parsing
- Lua table parser is basic - may fail on complex expressions
- No support for computed/function values in node definitions
- Groups stored with raw keys (may include unquoted strings)

### Performance
- No spatial acceleration for face culling (checks all neighbors)
- Large worlds (>1M nodes) can take several minutes to export
- Texture resolution per node (no atlas/packing)

### Texture Resolution
- Depends on textures being present in specified `--textures` directories
- Texture modifiers applied sequentially (can be slow for complex chains)
- No caching between runs (re-resolves all textures each export)

---

## Usage

### Export Command Example
```bash
python3 export_map.py \
  --db map.sqlite \
  --nodes nodes_full.lua \
  --textures /path/to/mods \
  --output map_export.obj \
  --min-node -100 -100 -100 \
  --max-node 100 100 100
```

### Dump Node Definitions
In-game: `/dumpnodes`
Output: `<worldpath>/nodes.lua`
