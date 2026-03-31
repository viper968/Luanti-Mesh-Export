#!/bin/python
import argparse
import struct
import json
import zlib
import zstandard as zstd

def decompress_blob(blob, max_output_size=1<<20):
    """
    Reads the first byte as the serialization version.
    - For version <29 (except 28) use zlib.
    - For version 28, if the payload doesn't start with 0x78 (typical for zlib),
      assume it's uncompressed.
    - For version 29, use zstd.
    Returns: (version, decompressed_payload)
    """
    if len(blob) < 1:
        raise ValueError("Empty blob")
    version = blob[0]
    payload = blob[1:]
    print(blob)
    if version < 29 and version != 28:
        try:
            data = zlib.decompress(payload)
        except Exception as e:
            raise RuntimeError(f"zlib decompression failed for version {version}: {e}")
    elif version == 28:
        if len(payload) > 0 and payload[0] == 0x78:
            try:
                data = zlib.decompress(payload)
            except Exception as e:
                raise RuntimeError(f"zlib decompression failed for version {version}: {e}")
        else:
            # Assume uncompressed payload for version 28
            data = payload
    elif version in [29, 83]:
        try:
            dctx = zstd.ZstdDecompressor()
            data = dctx.decompress(payload, max_output_size=max_output_size)
        except Exception as e:
            raise RuntimeError(f"zstd decompression failed for version {version}: {e}")
    else:
        raise ValueError(f"Unsupported serialization version: {version}")
    return version, data

def deserialize_mapblock(data):
    """
    Deserialize a decompressed MapBlock (assumed to be version 29 format).
    The structure (from Minetest's world_format.txt and serialization.h comments) is:
      u8  version              [already removed]
      u8  flags                -> 1 byte
      u16 lighting_complete    -> 2 bytes (big-endian)
      u32 timestamp            -> 4 bytes (big-endian)
      u8  mapping_version      -> 1 byte (should be 0)
      u16 num_mappings         -> 2 bytes (big-endian)
      For each mapping:
         u16 id              -> 2 bytes
         u16 name_len        -> 2 bytes
         u8[name_len] name   -> name_len bytes (UTF-8 string)
      u8  content_width        -> 1 byte (typically 2)
      u8  params_width         -> 1 byte (typically 2)
      Then, the node data:
         If content_width == 2: then
             - 4096 * u16 (param0) (big-endian)
             - 4096 * u8  (param1)
             - 4096 * u8  (param2)
         Total node data size: 4096 * (2 + 1 + 1) = 16384 bytes.
    (Note: There is further data for metadata, static objects, etc. which we skip here.)
    Returns a dictionary with header fields and a 3D list "nodes" indexed as [z][y][x]
    where each node is a dict with keys: "param0", "param1", "param2".
    """
    offset = 0
    header = {}
    # flags: 1 byte
    header["flags"] = data[offset]
    offset += 1
    # lighting_complete: 2 bytes big-endian
    header["lighting_complete"], = struct.unpack(">H", data[offset:offset+2])
    offset += 2
    # timestamp: 4 bytes
    header["timestamp"], = struct.unpack(">I", data[offset:offset+4])
    offset += 4
    # mapping_version: 1 byte
    header["mapping_version"] = data[offset]
    offset += 1
    # number of mappings: 2 bytes
    num_mappings, = struct.unpack(">H", data[offset:offset+2])
    header["num_mappings"] = num_mappings
    offset += 2
    mapping = {}
    for _ in range(num_mappings):
        node_id, = struct.unpack(">H", data[offset:offset+2])
        offset += 2
        name_len, = struct.unpack(">H", data[offset:offset+2])
        offset += 2
        name = data[offset:offset+name_len].decode('utf-8')
        offset += name_len
        mapping[node_id] = name
    header["mapping"] = mapping
    # content_width and params_width: each 1 byte
    header["content_width"] = data[offset]
    offset += 1
    header["params_width"] = data[offset]
    offset += 1

    # Now, node data:
    total_nodes = 16 * 16 * 16  # 4096
    nodes = []
    # For our purposes, we create a 3D list: nodes[z][y][x]
    # We'll first read param0, param1, param2 arrays.
    if header["content_width"] == 2:
        # param0: 4096 u16 values, big-endian
        fmt = ">" + "H" * total_nodes
        size_param0 = total_nodes * 2
        param0 = struct.unpack(fmt, data[offset:offset+size_param0])
        offset += size_param0
    else:
        # If content_width is 1:
        fmt = ">" + "B" * total_nodes
        size_param0 = total_nodes
        param0 = struct.unpack(fmt, data[offset:offset+size_param0])
        offset += size_param0
    # param1: 4096 u8 values
    fmt = ">" + "B" * total_nodes
    size_param1 = total_nodes
    param1 = struct.unpack(fmt, data[offset:offset+size_param1])
    offset += size_param1
    # param2: 4096 u8 values
    fmt = ">" + "B" * total_nodes
    size_param2 = total_nodes
    param2 = struct.unpack(fmt, data[offset:offset+size_param2])
    offset += size_param2

    # Build 3D array for convenience: using z as outer loop.
    nodes = [[[{
        "param0": param0[z * 256 + y * 16 + x],
        "param1": param1[z * 256 + y * 16 + x],
        "param2": param2[z * 256 + y * 16 + x]
    } for x in range(16)] for y in range(16)] for z in range(16)]

    return {
        "header": header,
        "nodes": nodes
    }

def main():
    parser = argparse.ArgumentParser(
        description="Decompress and deserialize a Minetest MapBlock binary blob")
    parser.add_argument("--input", required=True,
                        help="Path to the input binary file containing a MapBlock blob")
    parser.add_argument("--output-json", required=False,
                        help="Path to an output JSON file (optional)")
    args = parser.parse_args()
    with open(args.input, "rb") as f:
        blob = f.read()
    try:
        version, decompressed = decompress_blob(blob)
        print(f"Block serialization version: {version}")
    except Exception as e:
        print(f"Error during decompression: {e}")
        return
    try:
        block_data = deserialize_mapblock(decompressed)
    except Exception as e:
        print(f"Error during deserialization: {e}")
        return
    # For demonstration, print header info:
    print("Header:")
    for key, value in block_data["header"].items():
        if key == "mapping":
            print(f"  {key}:")
            for k, v in value.items():
                print(f"    {k} -> {v}")
        else:
            print(f"  {key}: {value}")
    # Optionally save the deserialized data as JSON.
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(block_data, f, indent=2)
        print(f"Deserialized data written to {args.output_json}")
    else:
        # Otherwise, print out a sample node at (0,0,0)
        sample = block_data["nodes"][0][0][0]
        print("Sample node at (0,0,0):", sample)

if __name__ == "__main__":
    main()
