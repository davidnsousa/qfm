"""3D model previews: STL, OBJ and PLY meshes read with numpy and drawn shaded from a fixed
three-quarter view: with cairo, farthest triangles first (painter's algorithm); a mesh too big
for that is drawn as one depth-tested dot per triangle (its triangles are about a pixel anyway)."""
import re

import cairo

try:
    import numpy as np
except ImportError:      # (python-numpy) no 3D previews without it
    np = None

EXTENSIONS = (".stl", ".obj", ".ply")
SIZE_LIMIT = 200 << 20         # bigger model files aren't previewed
DRAW_LIMIT = 150000            # triangles drawn as triangles at most (more: as dots)
RENDER_SIZE = 900              # the picture's longer side
COLOR = (0.62, 0.70, 0.82)     # the model's surface, lit
BACKGROUND = (0.16, 0.17, 0.19)


def is_model(name):
    return np is not None and name.lower().endswith(EXTENSIONS)


def render(path):
    """(cairo surface, info rows) for the model at path."""
    vertices, triangles, up_z = load(path)
    if len(triangles) == 0:
        raise ValueError("No triangles in this model")
    size = vertices.max(axis=0) - vertices.min(axis=0)
    rows = [("Vertices", f"{len(vertices):,}"), ("Triangles", f"{len(triangles):,}"),
            ("Model size", " × ".join(f"{v:.4g}" for v in size))]
    return draw(vertices, triangles, up_z), rows


# -- reading ------------------------------------------------------------------

def load(path):
    """(vertices float64 N×3, triangles int64 M×3, Z is up)."""
    name = path.lower()
    if name.endswith(".stl"):
        return (*load_stl(path), True)
    if name.endswith(".obj"):
        return (*load_obj(path), False)
    return (*load_ply(path), True)


def load_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        count = int(np.frombuffer(data, "<u4", 1, 80)[0])
        if 84 + 50 * count == len(data):           # binary (some start with "solid" too)
            records = np.frombuffer(data, np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                                    count, 84)
            vertices = records["v"].reshape(-1, 3).astype(np.float64)
            return vertices, np.arange(len(vertices)).reshape(-1, 3)
    numbers = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    vertices = np.array(numbers, dtype=np.float64).reshape(-1, 3)
    vertices = vertices[:len(vertices) // 3 * 3]
    return vertices, np.arange(len(vertices)).reshape(-1, 3)


def load_obj(path):
    vertices, triangles = [], []
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"v "):
                vertices.append(line.split()[1:4])
            elif line.startswith(b"f "):
                # "f 1 2 3 4", "f 1/1/1 ...", negative indices count back from the last vertex
                ids = [int(p.split(b"/")[0]) for p in line.split()[1:]]
                ids = [i - 1 if i > 0 else len(vertices) + i for i in ids]
                triangles += [(ids[0], ids[k], ids[k + 1]) for k in range(1, len(ids) - 1)]   # a fan
    return np.array(vertices, dtype=np.float64).reshape(-1, 3), np.array(triangles, dtype=np.int64).reshape(-1, 3)


PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
             "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
             "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def load_ply(path):
    with open(path, "rb") as f:
        if f.readline().strip() != b"ply":
            raise ValueError("Not a PLY file")
        fmt, elements = None, []          # elements: [name, count, [(property, type, list count type)]]
        while True:
            line = f.readline()
            if not line:
                raise ValueError("The PLY header has no end")
            words = line.decode("ascii", "replace").split()
            if not words or words[0] in ("comment", "obj_info"):
                continue
            if words[0] == "format":
                fmt = words[1]
            elif words[0] == "element":
                elements.append([words[1], int(words[2]), []])
            elif words[0] == "property":
                if words[1] == "list":
                    elements[-1][2].append((words[4], PLY_TYPES[words[3]], PLY_TYPES[words[2]]))
                else:
                    elements[-1][2].append((words[2], PLY_TYPES[words[1]], None))
            elif words[0] == "end_header":
                break
        body = f.read()
    if fmt == "ascii":
        return _ply_ascii(body, elements)
    return _ply_binary(body, elements, ">" if fmt == "binary_big_endian" else "<")


def _ply_ascii(body, elements):
    lines = iter(body.split(b"\n"))
    vertices = triangles = None
    for name, count, props in elements:
        rows = [next(lines).split() for _ in range(count)]
        if name == "vertex":
            names = [p[0] for p in props]
            columns = [names.index(axis) for axis in ("x", "y", "z")]
            vertices = np.array([[r[c] for c in columns] for r in rows], dtype=np.float64).reshape(-1, 3)
        elif name == "face":
            triangles = [(int(r[1]), int(r[k]), int(r[k + 1])) for r in rows for k in range(2, int(r[0]))]
    return vertices, np.array(triangles or [], dtype=np.int64).reshape(-1, 3)


def _ply_binary(body, elements, order):
    vertices = triangles = None
    at = 0
    for name, count, props in elements:
        if all(p[2] is None for p in props):           # fixed-size records: read them all at once
            dtype = np.dtype([(p[0], order + p[1]) for p in props])
            records = np.frombuffer(body, dtype, count, at)
            at += dtype.itemsize * count
            if name == "vertex":
                vertices = np.stack([records[a].astype(np.float64) for a in ("x", "y", "z")], axis=1)
            continue
        if name != "face" or len(props) != 1:
            raise ValueError(f"Can't read this PLY's {name} element")
        _, item, counter = props[0]
        size_c, size_i = np.dtype(counter).itemsize, np.dtype(item).itemsize
        first = int(np.frombuffer(body, order + counter, 1, at)[0]) if count else 3
        record = np.dtype([("n", order + counter), ("i", order + item, first)])
        records = np.frombuffer(body, record, count, at) if count else None
        if count and (records["n"] == first).all():   # all faces the same size (the usual case)
            faces = records["i"].astype(np.int64).reshape(count, first)
            at += record.itemsize * count
            triangles = np.concatenate([faces[:, [0, k, k + 1]] for k in range(1, first - 1)])
        else:                                          # mixed sizes: walk them
            triangles = []
            for _ in range(count):
                n = int(np.frombuffer(body, order + counter, 1, at)[0])
                f = np.frombuffer(body, order + item, n, at + size_c).tolist()
                triangles += [(f[0], f[k], f[k + 1]) for k in range(1, n - 1)]   # a fan
                at += size_c + n * size_i
            triangles = np.array(triangles, dtype=np.int64).reshape(-1, 3)
    return vertices, triangles if triangles is not None else np.zeros((0, 3), np.int64)


# -- drawing ------------------------------------------------------------------

def draw(vertices, triangles, up_z):
    triangles = triangles[(triangles >= 0).all(axis=1) & (triangles < len(vertices)).all(axis=1)]
    v = vertices - (vertices.min(axis=0) + vertices.max(axis=0)) / 2
    if up_z:
        v = v[:, [0, 2, 1]] * (1, 1, -1)              # Z up → Y up
    yaw, pitch = np.radians(-35), np.radians(25)
    turn = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    tilt = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    v = v @ turn.T @ tilt.T

    corners = v[triangles]                                 # M×3×3
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > 0
    corners, normals = corners[keep], normals[keep] / lengths[keep, None]
    light = np.array([0.35, 0.55, 0.75]) / np.linalg.norm([0.35, 0.55, 0.75])
    shade = 0.3 + 0.7 * np.abs(normals @ light)            # both sides lit: windings vary
    order = np.argsort(corners[:, :, 2].mean(axis=1))      # farthest (smallest z) first

    xy = corners[:, :, :2] * (1, -1)                       # cairo's y grows downwards
    low, high = xy.reshape(-1, 2).min(axis=0), xy.reshape(-1, 2).max(axis=0)
    margin = RENDER_SIZE * 0.05
    scale = (RENDER_SIZE - 2 * margin) / (max(high - low) or 1)
    width, height = ((high - low) * scale + 2 * margin).astype(int) + 1   # the picture fits the model
    xy = (xy - low) * scale + margin
    if len(xy) > DRAW_LIMIT:
        return _dots(xy, corners[:, :, 2].mean(axis=1), shade, int(width), int(height))
    points = xy[order].tolist()
    shades = shade[order].tolist()

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*BACKGROUND)
    cr.paint()
    cr.set_antialias(cairo.ANTIALIAS_NONE)     # no seams between neighboring triangles
    r, g, b = COLOR
    for (a, b_, c), s in zip(points, shades):
        cr.move_to(*a)
        cr.line_to(*b_)
        cr.line_to(*c)
        cr.close_path()
        cr.set_source_rgb(r * s, g * s, b * s)
        cr.fill()
    surface.flush()
    return surface


def _dots(xy, depth, shade, width, height):
    """Each triangle as a square dot at its center (as big as its triangles are on average), the
    nearest one winning each pixel."""
    edge = np.linalg.norm(xy[:, 1] - xy[:, 0], axis=1).mean()
    size = int(min(3, max(1, np.ceil(edge))))
    center = xy.mean(axis=1).astype(np.int64)
    pixels, depths, shades = [], [], []
    for dx in range(size):
        for dy in range(size):
            x = np.clip(center[:, 0] + dx, 0, width - 1)
            y = np.clip(center[:, 1] + dy, 0, height - 1)
            pixels.append(y * width + x)
            depths.append(depth)
            shades.append(shade)
    pixels, depths, shades = np.concatenate(pixels), np.concatenate(depths), np.concatenate(shades)
    order = np.argsort(depths, kind="stable")              # nearest last: its write wins
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, width)
    image = np.zeros((height, stride // 4), np.uint32)
    image[:] = _argb(*BACKGROUND)
    color = np.array(COLOR)[None, :] * shades[order][:, None]
    flat = image.reshape(-1)
    rows, columns = np.divmod(pixels[order], width)
    flat[rows * (stride // 4) + columns] = _argb(color[:, 0], color[:, 1], color[:, 2])
    data = bytearray(image.tobytes())
    return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, width, height, stride)


def _argb(r, g, b):
    """Opaque cairo pixels (0xAARRGGBB) from 0…1 color parts (numbers or arrays)."""
    to = lambda c: (np.asarray(c) * 255).astype(np.uint32)
    return 0xFF000000 | (to(r) << 16) | (to(g) << 8) | to(b)
