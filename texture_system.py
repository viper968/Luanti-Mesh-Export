"""
Minetest/Luanti texture modifier system.

Parses texture strings like:
  "stone.png^overlay.png^[opacity:128]"
  "base.png^(layer1.png^[mask:mask.png])^[colorize:#ff0000:128]"

Applies modifiers using Pillow to produce final texture files.
"""

import os
import re
import hashlib
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageChops, ImageColor
except ImportError:
    Image = None


def ensure_pillow():
    if Image is None:
        raise ImportError(
            "Pillow is required for texture processing. "
            "Install with: pip install Pillow"
        )


def tokenize_texture_string(tex_str):
    """
    Parse a texture string into a list of layers.

    Handles:
      - ^ as layer separator
      - () for grouping
      - \ for escaping ^, :, (, )
      - [modifier:args as modifier tokens

    Returns a list where each element is either:
      - A string (filename or modifier)
      - A nested list (grouped with parentheses)
    """
    tokens = []
    i = 0
    length = len(tex_str)

    while i < length:
        if tex_str[i] == '\\':
            if i + 1 < length:
                tokens.append(tex_str[i + 1])
                i += 2
            else:
                i += 1

        elif tex_str[i] == '^':
            i += 1

        elif tex_str[i] == '(':
            depth = 1
            j = i + 1
            while j < length and depth > 0:
                if tex_str[j] == '\\' and j + 1 < length:
                    j += 2
                    continue
                if tex_str[j] == '(':
                    depth += 1
                elif tex_str[j] == ')':
                    depth -= 1
                j += 1
            inner = tex_str[i + 1:j - 1]
            tokens.append(tokenize_texture_string(inner))
            i = j

        elif tex_str[i] == ')':
            i += 1

        else:
            j = i
            while j < length and tex_str[j] not in '^()':
                if tex_str[j] == '\\' and j + 1 < length:
                    j += 2
                    continue
                j += 1
            token = tex_str[i:j]
            if token:
                tokens.append(token)
            i = j

    return tokens


def is_modifier(token):
    return isinstance(token, str) and token.startswith('[')


def resolve_texture_layer(tokens, texture_resolver):
    """
    Resolve a list of tokens into a single PIL Image.

    Tokens are processed left-to-right: each subsequent token is overlaid
    on top of the accumulated result.
    """
    base = None

    for token in tokens:
        if isinstance(token, list):
            layer = resolve_texture_layer(token, texture_resolver)
        elif is_modifier(token):
            layer = apply_modifier_to_base(token, base, texture_resolver)
            if layer is not None:
                base = layer
            continue
        else:
            layer = texture_resolver.load_texture(token)

        if layer is not None:
            if base is None:
                base = layer.copy()
            else:
                base = overlay_images(base, layer)

    return base


def apply_modifier_to_base(modifier_str, base, texture_resolver):
    """
    Apply a single modifier string to a base image.
    Modifier format: [modname:arg1:arg2:...]
    """
    if base is None:
        return None

    # Parse modifier name and arguments
    if modifier_str.startswith('['):
        modifier_str = modifier_str[1:]

    parts = split_modifier_args(modifier_str)
    if not parts:
        return base

    name = parts[0].lower()
    args = parts[1:]

    if name == 'opacity':
        return mod_opacity(base, args)
    elif name == 'noalpha':
        return mod_noalpha(base)
    elif name == 'colorize':
        return mod_colorize(base, args)
    elif name == 'colorizehsl':
        return mod_colorizehsl(base, args)
    elif name == 'multiply':
        return mod_multiply(base, args)
    elif name == 'screen':
        return mod_screen(base, args)
    elif name == 'invert':
        return mod_invert(base, args)
    elif name == 'brighten':
        return mod_brighten(base)
    elif name == 'makealpha':
        return mod_makealpha(base, args)
    elif name.startswith('transform'):
        return mod_transform(base, name)
    elif name == 'contrast':
        return mod_contrast(base, args)
    elif name == 'hsl':
        return mod_hsl(base, args)
    elif name == 'resize':
        return mod_resize(base, args)
    elif name == 'fill':
        return base
    elif name == 'mask':
        return mod_mask(base, args, texture_resolver)
    elif name == 'overlay':
        return mod_overlay_blend(base, args, texture_resolver)
    elif name == 'hardlight':
        return mod_hardlight_blend(base, args, texture_resolver)
    elif name == 'lowpart':
        return mod_lowpart(base, args, texture_resolver)
    elif name == 'sheet':
        return mod_sheet(base, args)
    elif name == 'verticalframe':
        return mod_verticalframe(base, args)
    elif name == 'combine':
        return mod_combine(args, texture_resolver)
    elif name == 'crack' or name == 'cracko':
        return base
    elif name == 'inventorycube':
        return base
    elif name == 'png':
        return mod_png(args)
    else:
        return base


def split_modifier_args(s):
    """Split modifier string by unescaped colons."""
    parts = []
    current = []
    i = 0
    length = len(s)

    while i < length:
        if s[i] == '\\' and i + 1 < length:
            current.append(s[i + 1])
            i += 2
        elif s[i] == ':':
            parts.append(''.join(current))
            current = []
            i += 1
        else:
            current.append(s[i])
            i += 1

    parts.append(''.join(current))
    return parts


def overlay_images(base, overlay):
    if base is None:
        return overlay.copy() if overlay else None
    if overlay is None:
        return base.copy()

    if overlay.size != base.size:
        overlay = overlay.resize(base.size, Image.NEAREST)

    base = base.convert('RGBA')
    overlay = overlay.convert('RGBA')

    import numpy as np
    dst = np.array(base, dtype=np.float64)
    src = np.array(overlay, dtype=np.float64)

    dst_a = dst[:, :, 3]
    src_a = src[:, :, 3]

    result = dst.copy()

    # Case 1: src fully transparent -> keep dst
    # Case 2: src fully opaque OR dst fully transparent -> replace with src
    replace = (src_a == 255) | (dst_a == 0)
    result[replace] = src[replace]

    # Case 3: semi-transparent src on opaque dst -> lerp RGB, alpha stays 255
    semi_on_opaque = (src_a > 0) & (src_a < 255) & (dst_a == 255)
    if np.any(semi_on_opaque):
        mask = semi_on_opaque
        t = src_a[mask] / 255.0
        for c in range(3):
            result[mask, c] = dst[mask, c] * (1 - t) + src[mask, c] * t
        result[mask, 3] = 255

    # Case 4: both semi-transparent -> lerp RGB, special alpha formula
    both_semi = (src_a > 0) & (src_a < 255) & (dst_a > 0) & (dst_a < 255)
    if np.any(both_semi):
        mask = both_semi
        t = src_a[mask] / 255.0
        for c in range(3):
            result[mask, c] = dst[mask, c] * (1 - t) + src[mask, c] * t
        # Minetest's weird alpha formula: dst_a + (255 - dst_a) * src_a² / 255²
        result[mask, 3] = dst_a[mask] + (255 - dst_a[mask]) * (src_a[mask] ** 2) / (255 * 255)

    return Image.fromarray(np.clip(result, 0, 255).astype(np.uint8), 'RGBA')


# ============================================================================
# Modifier implementations
# ============================================================================

def parse_color(color_str):
    """Parse a Minetest ColorString to RGB(A) tuple."""
    color_str = color_str.strip()
    if color_str.startswith('#'):
        hex_str = color_str[1:]
        if len(hex_str) == 6:
            r = int(hex_str[0:2], 16)
            g = int(hex_str[2:4], 16)
            b = int(hex_str[4:6], 16)
            return (r, g, b)
        elif len(hex_str) == 8:
            r = int(hex_str[0:2], 16)
            g = int(hex_str[2:4], 16)
            b = int(hex_str[4:6], 16)
            a = int(hex_str[6:8], 16)
            return (r, g, b, a)
    # Try named colors
    try:
        return ImageColor.getrgb(color_str)
    except ValueError:
        return (255, 255, 255)


def mod_opacity(img, args):
    """[opacity:<r>] - Set opacity (0-255)."""
    if not args:
        return img
    try:
        opacity = int(args[0])
    except ValueError:
        return img

    img = img.convert('RGBA')
    r, g, b, a = img.split()
    # Scale alpha channel by opacity/255
    a = a.point(lambda x: int(x * opacity / 255))
    return Image.merge('RGBA', (r, g, b, a))


def mod_noalpha(img):
    """[noalpha] - Make completely opaque."""
    img = img.convert('RGBA')
    r, g, b, _ = img.split()
    a = Image.new('L', img.size, 255)
    return Image.merge('RGBA', (r, g, b, a))


def mod_colorize(img, args):
    """[colorize:<color>:<ratio>] - Colorize texture."""
    if not args:
        return img

    color_str = args[0]
    color = parse_color(color_str)
    r, g, b = color[0], color[1], color[2]

    ratio = 128
    if len(args) > 1:
        try:
            ratio_val = args[1]
            if ratio_val.lower() == 'alpha' and len(color) > 3:
                ratio = color[3]
            else:
                ratio = int(ratio_val)
        except ValueError:
            ratio = 128

    img = img.convert('RGBA')
    # Create a solid color image
    color_img = Image.new('RGBA', img.size, (r, g, b, 255))
    # Blend based on ratio (0 = original, 255 = color)
    alpha = ratio / 255.0
    result = Image.blend(img, color_img, alpha)
    # Preserve original alpha
    _, _, _, orig_a = img.split()
    result_r, result_g, result_b, _ = result.split()
    return Image.merge('RGBA', (result_r, result_g, result_b, orig_a))


def mod_colorizehsl(img, args):
    """[colorizehsl:<hue>:<saturation>:<lightness>]"""
    if not args:
        return img

    try:
        hue = int(args[0])
    except ValueError:
        return img
    sat = int(args[1]) if len(args) > 1 else 50
    light = int(args[2]) if len(args) > 2 else 0

    img = img.convert('RGBA')
    # Convert to grayscale-ish then apply HSL
    # This is a simplified version - real Minetest does proper HSL conversion
    r, g, b, a = img.split()
    gray = img.convert('L')
    gray_rgba = gray.convert('RGBA')

    # Approximate: adjust hue by rotating RGB channels
    h_shift = hue / 360.0
    result = adjust_hue_simple(gray_rgba, h_shift)
    return Image.merge('RGBA', (*result.split()[:3], a))


def adjust_hue_simple(img, hue_shift):
    """Simple hue rotation approximation."""
    img = img.convert('HSV')
    h, s, v = img.split()
    # Shift hue
    h = h.point(lambda x: int((x + hue_shift * 255) % 255))
    # Adjust saturation
    img = Image.merge('HSV', (h, s, v))
    return img.convert('RGBA')


def mod_multiply(img, args):
    """[multiply:<color>] - Multiply blend."""
    if not args:
        return img
    color = parse_color(args[0])
    img = img.convert('RGBA')
    # Create color image and multiply
    color_img = Image.new('RGBA', img.size, (*color[:3], 255))
    return ImageChops.multiply(img, color_img)


def mod_screen(img, args):
    """[screen:<color>] - Screen blend."""
    if not args:
        return img
    color = parse_color(args[0])
    img = img.convert('RGBA')
    color_img = Image.new('RGBA', img.size, (*color[:3], 255))
    return ImageChops.screen(img, color_img)


def mod_invert(img, args):
    """[invert:<mode>] - Invert channels (r,g,b,a)."""
    if not args:
        return img
    mode = args[0].lower()
    img = img.convert('RGBA')
    r, g, b, a = img.split()
    if 'r' in mode:
        r = ImageChops.invert(r)
    if 'g' in mode:
        g = ImageChops.invert(g)
    if 'b' in mode:
        b = ImageChops.invert(b)
    if 'a' in mode:
        a = ImageChops.invert(a)
    return Image.merge('RGBA', (r, g, b, a))


def mod_brighten(img):
    """[brighten] - Brighten texture."""
    img = img.convert('RGBA')
    enhancer = ImageEnhance.Brightness(img)
    return enhancer.enhance(1.3)


def mod_makealpha(img, args):
    """[makealpha:<r>,<g>,<b>] - Make color transparent."""
    if not args:
        return img
    parts = args[0].split(',')
    if len(parts) < 3:
        return img
    try:
        tr, tg, tb = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return img

    img = img.convert('RGBA')
    pixels = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if r == tr and g == tg and b == tb:
                pixels[x, y] = (r, g, b, 0)
    return img


def mod_transform(img, name):
    """[transformI/R90/R180/R270/FX/FY/FXR90/FYR90]"""
    img = img.convert('RGBA')
    if name == 'transformi' or name == 'transformI':
        return img
    elif name == 'transformr90' or name == 'transformR90':
        return img.transpose(Image.ROTATE_90)
    elif name == 'transformr180' or name == 'transformR180':
        return img.transpose(Image.ROTATE_180)
    elif name == 'transformr270' or name == 'transformR270':
        return img.transpose(Image.ROTATE_270)
    elif name == 'transformfx' or name == 'transformFX':
        return img.transpose(Image.FLIP_LEFT_RIGHT)
    elif name == 'transformfy' or name == 'transformFY':
        return img.transpose(Image.FLIP_TOP_BOTTOM)
    elif name == 'transformfxr90' or name == 'transformFXR90':
        return img.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.ROTATE_90)
    elif name == 'transformfyr90' or name == 'transformFYR90':
        return img.transpose(Image.FLIP_TOP_BOTTOM).transpose(Image.ROTATE_90)
    return img


def mod_contrast(img, args):
    """[contrast:<contrast>:<brightness>]"""
    if not args:
        return img
    try:
        contrast = int(args[0])
    except ValueError:
        return img

    img = img.convert('RGBA')
    factor = (259 * (contrast + 255)) / (255 * (259 - contrast))

    def adjust(c):
        return max(0, min(255, int(factor * (c - 128) + 128)))

    r, g, b, a = img.split()
    r = r.point(adjust)
    g = g.point(adjust)
    b = b.point(adjust)

    if len(args) > 1:
        try:
            brightness = int(args[1])
            brightness_factor = 1.0 + brightness / 127.0
            enhancer = ImageEnhance.Brightness(Image.merge('RGBA', (r, g, b, a)))
            img = enhancer.enhance(brightness_factor)
            r, g, b, a = img.split()
        except ValueError:
            pass

    return Image.merge('RGBA', (r, g, b, a))


def mod_hsl(img, args):
    """[hsl:<hue>:<saturation>:<lightness>]"""
    if not args:
        return img
    try:
        hue = int(args[0])
    except ValueError:
        return img
    sat = int(args[1]) if len(args) > 1 else 0
    light = int(args[2]) if len(args) > 2 else 0

    img = img.convert('RGBA')
    # Simplified HSL adjustment using Pillow
    r, g, b, a = img.split()

    # Brightness/lightness
    if light != 0:
        factor = 1.0 + light / 100.0
        enhancer = ImageEnhance.Brightness(Image.merge('RGB', (r, g, b)))
        img = enhancer.enhance(factor)
        r, g, b = img.split()

    # Contrast/saturation approximation
    if sat != 0:
        factor = 1.0 + sat / 50.0
        enhancer = ImageEnhance.Color(Image.merge('RGB', (r, g, b)))
        img = enhancer.enhance(factor)
        r, g, b = img.split()

    return Image.merge('RGBA', (r, g, b, a))


def mod_resize(img, args):
    """[resize:<w>x<h>]"""
    if not args:
        return img
    dims = args[0].split('x')
    if len(dims) != 2:
        return img
    try:
        w, h = int(dims[0]), int(dims[1])
    except ValueError:
        return img
    return img.resize((w, h), Image.NEAREST)


def mod_mask(img, args, texture_resolver):
    """[mask:<file>] - Bitwise AND mask (matches Minetest dst.color &= mask.color)."""
    if not args or not texture_resolver:
        return img
    mask_tex = texture_resolver.load_texture(args[0])
    if mask_tex is None:
        return img

    img = img.convert('RGBA')
    mask_tex = mask_tex.convert('RGBA')
    if mask_tex.size != img.size:
        mask_tex = mask_tex.resize(img.size, Image.NEAREST)

    import numpy as np
    img_arr = np.array(img)
    mask_arr = np.array(mask_tex)
    result_arr = img_arr & mask_arr
    return Image.fromarray(result_arr, 'RGBA')


def mod_overlay_blend(img, args, texture_resolver):
    """[overlay:<file>] - Overlay blend mode."""
    if not args or not texture_resolver:
        return img
    overlay = texture_resolver.load_texture(args[0])
    if overlay is None:
        return img

    img = img.convert('RGBA')
    overlay = overlay.convert('RGBA')
    if overlay.size != img.size:
        overlay = overlay.resize(img.size, Image.NEAREST)

    # Simplified overlay blend
    result = Image.blend(img, overlay, 0.5)
    return result


def mod_hardlight_blend(img, args, texture_resolver):
    """[hardlight:<file>] - Hard light blend mode."""
    if not args or not texture_resolver:
        return img
    return mod_overlay_blend(img, args, texture_resolver)


def mod_lowpart(img, args, texture_resolver):
    """[lowpart:<percent>:<file>] - Overlay lower portion."""
    if len(args) < 2 or not texture_resolver:
        return img
    try:
        percent = int(args[0])
    except ValueError:
        return img

    overlay = texture_resolver.load_texture(args[1])
    if overlay is None:
        return img

    img = img.convert('RGBA')
    overlay = overlay.convert('RGBA')
    if overlay.size != img.size:
        overlay = overlay.resize(img.size, Image.NEAREST)

    w, h = img.size
    split_y = int(h * (100 - percent) / 100)

    # Create mask: transparent above split, opaque below
    mask = Image.new('L', (w, h), 0)
    for y in range(split_y, h):
        for x in range(w):
            mask.putpixel((x, y), 255)

    # Composite
    result = img.copy()
    result.paste(overlay, (0, 0), mask)
    return result


def mod_sheet(img, args):
    """[sheet:<w>x<h>:<x>,<y>] - Extract tile from spritesheet."""
    if not args:
        return img
    dims = args[0].split('x')
    if len(dims) != 2:
        return img
    try:
        cols, rows = int(dims[0]), int(dims[1])
    except ValueError:
        return img

    pos = [0, 0]
    if len(args) > 1:
        coords = args[1].split(',')
        if len(coords) >= 2:
            try:
                pos = [int(coords[0]), int(coords[1])]
            except ValueError:
                pass

    w, h = img.size
    tile_w = w // cols
    tile_h = h // rows
    left = pos[0] * tile_w
    upper = pos[1] * tile_h
    return img.crop((left, upper, left + tile_w, upper + tile_h))


def mod_verticalframe(img, args):
    """[verticalframe:<t>:<n>] - Extract animation frame."""
    if len(args) < 2:
        return img
    try:
        frame_count = int(args[0])
        frame_num = int(args[1])
    except ValueError:
        return img

    w, h = img.size
    frame_h = h // frame_count
    return img.crop((0, frame_num * frame_h, w, (frame_num + 1) * frame_h))


def mod_combine(args, texture_resolver):
    """[combine:<w>x<h>:<x>,<y>=<file>:...]"""
    if not args or not texture_resolver:
        return None
    dims = args[0].split('x')
    if len(dims) != 2:
        return None
    try:
        w, h = int(dims[0]), int(dims[1])
    except ValueError:
        return None

    result = Image.new('RGBA', (w, h), (0, 0, 0, 0))

    for placement in args[1:]:
        if '=' not in placement:
            continue
        pos_str, filename = placement.split('=', 1)
        coords = pos_str.split(',')
        if len(coords) < 2:
            continue
        try:
            x, y = int(coords[0]), int(coords[1])
        except ValueError:
            continue

        layer = texture_resolver.load_texture(filename)
        if layer:
            result.paste(layer, (x, y))

    return result


def mod_png(args):
    """[png:<base64>] - Embedded base64 PNG."""
    if not args:
        return None
    import base64
    import io
    try:
        data = base64.b64decode(args[0])
        return Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception:
        return None


# ============================================================================
# Texture Resolver
# ============================================================================

class TextureResolver:
    """Finds and loads texture files from mod directories."""

    def __init__(self, texture_dirs):
        self.texture_dirs = texture_dirs
        self.cache = {}
        self.path_index = {}
        self._build_index()

    def _build_index(self):
        for tex_dir in self.texture_dirs:
            if not os.path.isdir(tex_dir):
                continue
            for root, dirs, files in os.walk(tex_dir):
                for f in files:
                    if f.endswith(('.png', '.jpg', '.tga')):
                        # Index by filename (without extension too)
                        self.path_index.setdefault(f, [])
                        self.path_index[f].append(os.path.join(root, f))
                        stem = os.path.splitext(f)[0]
                        if stem != f:
                            self.path_index.setdefault(stem, [])
                            self.path_index[stem].append(os.path.join(root, f))

    def find_texture(self, name):
        """Find the file path for a texture name."""
        if not name:
            return None
        # Strip extension for lookup
        stem = os.path.splitext(name)[0]

        # Try exact match first
        if name in self.path_index:
            return self.path_index[name][0]
        if stem in self.path_index:
            return self.path_index[stem][0]

        # Try with .png extension
        png_name = stem + '.png'
        if png_name in self.path_index:
            return self.path_index[png_name][0]

        return None

    def load_texture(self, name):
        """Load a texture, returning a PIL Image or None."""
        ensure_pillow()

        if name in self.cache:
            return self.cache[name]

        path = self.find_texture(name)
        if path:
            try:
                img = Image.open(path).convert('RGBA')
                self.cache[name] = img
                return img
            except Exception:
                pass

        self.cache[name] = None
        return None


# ============================================================================
# Public API
# ============================================================================

def resolve_texture(tex_str, texture_resolver):
    """
    Resolve a complete texture string to a PIL Image.

    Args:
        tex_str: Texture string like "stone.png^[opacity:128]^overlay.png"
        texture_resolver: TextureResolver instance

    Returns:
        PIL Image or None
    """
    ensure_pillow()

    if not tex_str or tex_str == 'unknown':
        return None

    # Clean up the string
    tex_str = tex_str.strip()

    # Parse into tokens
    tokens = tokenize_texture_string(tex_str)

    # Resolve all layers
    return resolve_texture_layer(tokens, texture_resolver)


def get_base_texture_names(tex_str):
    """
    Extract all base texture filenames from a texture string.
    Useful for dependency tracking.
    """
    if not tex_str:
        return []

    names = set()
    tokens = tokenize_texture_string(tex_str)

    def collect(toks):
        for t in toks:
            if isinstance(t, list):
                collect(t)
            elif isinstance(t, str) and not is_modifier(t):
                # Strip extension
                names.add(os.path.splitext(t)[0])
                names.add(t)

    collect(tokens)
    return list(names)
