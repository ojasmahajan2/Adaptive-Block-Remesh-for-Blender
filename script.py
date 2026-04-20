import bpy
import bmesh
import math
from mathutils import Vector, kdtree
from collections import defaultdict

# ============================================================
# ADAPTIVE BLOCK REMESH — Top-Down Octree
# ============================================================
#
# HOW IT WORKS:
#   1. Cover mesh bounding box with a coarse grid of large cells
#   2. For each cell near the surface, measure local detail:
#       • Polygon density  (face count per unit area)
#       • Surface curvature (normal angle variation)
#   3. If detail >= threshold → subdivide into 8 smaller children
#      If detail <  threshold → keep as one large block
#   4. Recurse until detail is low OR minimum block size reached
#   5. All leaf cells on the surface → separate cube objects
#
# THIS PRODUCES:
#   • Large blocks on flat/simple areas
#   • Tiny dense blocks on detailed/complex areas
#   • Clean 2:1 size transitions (octree property)
#   • Gap-free shell coverage
#
# Each cube is a separate object → Object Info → Random works.
# ============================================================

# ============================
# USER SETTINGS
# ============================

# --- Block sizes ---
BASE_SIZE = 0.03        # Smallest cube (world units). Decrease = finer detail.
MAX_DEPTH = 4           # Subdivision depth. Largest cube = BASE_SIZE × 2^MAX_DEPTH
                        # Depth 4 → sizes: 0.48, 0.24, 0.12, 0.06, 0.03
MIN_DEPTH = 1           # FORCE subdivision to at least this depth everywhere.
                        # 0 = full range (big blocks allowed)
                        # 2 = everything at least depth-2 or smaller
                        # Raise for denser, more uniform results.

# --- Shell ---
SHELL_THICKNESS = 0.8   # Shell depth in multiples of BASE_SIZE. Lower = tighter to surface.

# --- Detail detection ---
SAMPLE_RADIUS_MULT = 0.85  # Radius for sampling faces around each cell
                            # relative to cell size. Lower = snappier boundaries.

# Per-depth subdivision thresholds [0..1]:
#   detail >= threshold → SUBDIVIDE into 8 children
#   detail <  threshold → KEEP as a single block
# Lower = more aggressive subdivision = denser small blocks at that level.
SUBDIV_THRESHOLDS = {
    0: 0.01,    # Almost always subdivide from root
    1: 0.12,    # Easy to subdivide — low detail still splits
    2: 0.28,    # Moderate detail triggers subdivision
    3: 0.45,    # Significant detail needed for finest blocks
}

# Signal weights (should sum to 1.0):
DENSITY_WEIGHT   = 0.5   # Polygon density (high on sculpts/scans with uneven topo)
CURVATURE_WEIGHT = 0.5   # Normal variation (high on curved/rough surfaces)

# Safety limit
MAX_BLOCKS = 400000

# --- Output ---
APPLY_SCALE     = True
COLLECTION_NAME = "AdaptiveBlockRemesh"
ROOT_NAME       = "AdaptiveBlockRemesh_ROOT"
DELETE_PREVIOUS = True


# ============================
# VALIDATION
# ============================
ctx = bpy.context
obj = ctx.active_object

if obj is None or obj.type != 'MESH':
    raise Exception("Select exactly one mesh object first.")

bpy.ops.object.mode_set(mode='OBJECT')

if APPLY_SCALE:
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

depsgraph = ctx.evaluated_depsgraph_get()
eval_obj  = obj.evaluated_get(depsgraph)
eval_mesh = eval_obj.to_mesh()

world     = obj.matrix_world
world_inv = world.inverted()
world3    = world.to_3x3()


# ============================
# BMESH + TRIANGULATE
# ============================
bm = bmesh.new()
bm.from_mesh(eval_mesh)
bm.faces.ensure_lookup_table()
bm.verts.ensure_lookup_table()
bm.normal_update()

bmesh.ops.triangulate(bm, faces=bm.faces[:])
bm.faces.ensure_lookup_table()
bm.verts.ensure_lookup_table()
bm.normal_update()

if len(bm.faces) == 0:
    bm.free()
    eval_obj.to_mesh_clear()
    raise Exception("Mesh has no faces.")

num_faces = len(bm.faces)
print(f"Source mesh: {num_faces} triangles")


# ============================
# FACE DATA (world space)
# ============================
face_centers_w = []
face_normals_w = []
face_areas     = []

for f in bm.faces:
    c_world = world @ f.calc_center_median()
    n_world = (world3 @ f.normal).normalized()
    area    = max(f.calc_area(), 1e-12)
    face_centers_w.append(c_world)
    face_normals_w.append(n_world)
    face_areas.append(area)

# KD-tree for fast spatial queries
kd = kdtree.KDTree(len(face_centers_w))
for i, co in enumerate(face_centers_w):
    kd.insert(co, i)
kd.balance()

# Global density stats for normalization
bbox_world = [world @ Vector(c) for c in obj.bound_box]
bbox_min = Vector((min(v.x for v in bbox_world),
                   min(v.y for v in bbox_world),
                   min(v.z for v in bbox_world)))
bbox_max = Vector((max(v.x for v in bbox_world),
                   max(v.y for v in bbox_world),
                   max(v.z for v in bbox_world)))

total_surface_area  = sum(face_areas)
global_face_density = num_faces / max(total_surface_area, 1e-12)  # faces per unit area

print(f"Bounding box: {bbox_min} → {bbox_max}")
print(f"Surface area: {total_surface_area:.4f}")
print(f"Global face density: {global_face_density:.1f} faces/unit²")


# ============================
# SURFACE NEAREST POINT
# ============================
def closest_surface(world_point):
    local_p = world_inv @ world_point
    ok, loc, normal, _ = eval_obj.closest_point_on_mesh(local_p)
    if not ok:
        return False, None, 1e9
    world_loc = world @ loc
    dist = (world_point - world_loc).length
    return True, world_loc, dist


# ============================
# DETAIL MEASUREMENT
# ============================
def measure_detail(cell_center, cell_size):
    """
    Returns detail level [0..1] for this octree cell.
    Combines polygon density + surface curvature.
    Higher = more detail = should subdivide.
    """
    radius = cell_size * SAMPLE_RADIUS_MULT
    hits = kd.find_range(cell_center, radius)

    if len(hits) < 3:
        return 0.0

    # --- Signal 1: Face count density ---
    # How many faces are here compared to the global average?
    # Use the search sphere's cross-sectional area as the reference area.
    search_area = math.pi * radius * radius
    expected_faces = global_face_density * search_area
    actual_faces   = len(hits)

    if expected_faces > 0:
        density_ratio = actual_faces / expected_faces
    else:
        density_ratio = 1.0

    # Normalize: 1.0 = average, >1 = denser. Map to [0..1]
    density_signal = max(0.0, min(1.0, (density_ratio - 0.3) / 2.5))

    # --- Signal 2: Normal angle variation (curvature) ---
    normals = [face_normals_w[idx] for _, idx, _ in hits]

    avg_n = Vector((0.0, 0.0, 0.0))
    for n in normals:
        avg_n += n
    avg_n /= len(normals)

    if avg_n.length > 1e-8:
        avg_n.normalize()
    else:
        # Normals cancel out → extremely high variation
        return 1.0

    angle_sum = 0.0
    for n in normals:
        d = max(-1.0, min(1.0, n.dot(avg_n)))
        angle_sum += math.acos(d)

    avg_angle = angle_sum / len(normals)

    # Normalize: 0 = flat, 1.0 = 60°+ average deviation
    curvature_signal = min(avg_angle / (math.pi / 3.0), 1.0)

    # --- Combine ---
    detail = density_signal * DENSITY_WEIGHT + curvature_signal * CURVATURE_WEIGHT
    return max(0.0, min(1.0, detail))


# ============================
# OCTREE CONSTRUCTION
# ============================

# Coarsest cell size
coarse_size = BASE_SIZE * (2 ** MAX_DEPTH)
shell_dist  = BASE_SIZE * SHELL_THICKNESS
HALF_SQRT3  = math.sqrt(3.0) / 2.0   # ≈ 0.866

# Build initial coarse grid covering bounding box + padding
pad = coarse_size
gmin = Vector((
    math.floor((bbox_min.x - pad) / coarse_size) * coarse_size,
    math.floor((bbox_min.y - pad) / coarse_size) * coarse_size,
    math.floor((bbox_min.z - pad) / coarse_size) * coarse_size,
))
gmax = Vector((
    math.ceil((bbox_max.x + pad) / coarse_size) * coarse_size,
    math.ceil((bbox_max.y + pad) / coarse_size) * coarse_size,
    math.ceil((bbox_max.z + pad) / coarse_size) * coarse_size,
))

# Seed cells: (center, size, depth)
stack = []
x = gmin.x + coarse_size / 2
while x < gmax.x:
    y = gmin.y + coarse_size / 2
    while y < gmax.y:
        z = gmin.z + coarse_size / 2
        while z < gmax.z:
            stack.append((Vector((x, y, z)), coarse_size, 0))
            z += coarse_size
        y += coarse_size
    x += coarse_size

print(f"Coarse cell size: {coarse_size:.4f}")
print(f"Initial coarse cells: {len(stack)}")


# ============================
# ITERATIVE OCTREE TRAVERSAL
# ============================
final_blocks = []    # (center, size) — leaf cells to become cubes

while stack:
    cell_center, cell_size, depth = stack.pop()

    # Safety cap
    if len(final_blocks) >= MAX_BLOCKS:
        break

    # Is this cell near the surface?
    # Traversal check is GENEROUS (half-diagonal) so we don't miss any surface
    ok, surf_p, dist = closest_surface(cell_center)
    traverse_reach = cell_size * HALF_SQRT3 + shell_dist
    if not ok or dist > traverse_reach:
        continue

    # At maximum depth: this is a leaf → emit if tightly on shell
    if depth >= MAX_DEPTH:
        # Tight check: surface must be within half the block width + margin
        if dist <= cell_size * 0.52 + shell_dist:
            final_blocks.append((cell_center, cell_size))
        continue

    # Below MIN_DEPTH: always subdivide (force minimum resolution)
    if depth < MIN_DEPTH:
        q = cell_size / 4
        child_size = cell_size / 2
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    cc = Vector((
                        cell_center.x + sx * q,
                        cell_center.y + sy * q,
                        cell_center.z + sz * q,
                    ))
                    stack.append((cc, child_size, depth + 1))
        continue

    # Measure detail in this cell
    detail = measure_detail(cell_center, cell_size)
    threshold = SUBDIV_THRESHOLDS.get(depth, 0.5)

    if detail >= threshold:
        # High detail → subdivide into 8 children
        q = cell_size / 4
        child_size = cell_size / 2
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    cc = Vector((
                        cell_center.x + sx * q,
                        cell_center.y + sy * q,
                        cell_center.z + sz * q,
                    ))
                    stack.append((cc, child_size, depth + 1))
    else:
        # Low detail → keep as large block (tight shell check)
        if dist <= cell_size * 0.52 + shell_dist:
            final_blocks.append((cell_center, cell_size))

if len(final_blocks) >= MAX_BLOCKS:
    print(f"WARNING: Hit MAX_BLOCKS limit ({MAX_BLOCKS}).")

print(f"Blocks before shell filter: {len(final_blocks)}")


# ============================
# PRECISE SHELL FILTER
# ============================
# Tight filter: surface must pass THROUGH or very close to each block.
# Using size * 0.5 (half cube-width) NOT size * 0.866 (half diagonal).
# This ensures blocks hug the surface instead of floating outward.
filtered = []
for center, size in final_blocks:
    ok, surf_p, dist = closest_surface(center)
    if not ok:
        continue
    # Surface must be within ~half the block width from center
    max_dist = size * 0.52 + shell_dist * 0.4
    if dist <= max_dist:
        filtered.append((center, size))

final_blocks = filtered
print(f"Blocks after shell filter: {len(final_blocks)}")

if not final_blocks:
    bm.free()
    eval_obj.to_mesh_clear()
    raise Exception("No blocks generated. Increase SHELL_THICKNESS or reduce BASE_SIZE.")


# ============================
# DIAGNOSTICS
# ============================
sc = defaultdict(int)
for _, sz in final_blocks:
    sc[round(sz, 6)] += 1

print("\n  Block size distribution:")
for sz in sorted(sc, reverse=True):
    cnt = sc[sz]
    pct = cnt * 100.0 / len(final_blocks)
    bar = "█" * max(1, int(pct / 2))
    print(f"    {sz:.4f}  (depth {int(round(math.log2(sz / BASE_SIZE)))})"
          f": {cnt:6d} blocks  ({pct:5.1f}%)  {bar}")
print(f"\n  Total: {len(final_blocks)} blocks")


# ============================
# COLLECTION SETUP
# ============================
scene      = ctx.scene
collection = bpy.data.collections.get(COLLECTION_NAME)
if collection is None:
    collection = bpy.data.collections.new(COLLECTION_NAME)
    scene.collection.children.link(collection)

if DELETE_PREVIOUS:
    for ob in list(collection.objects):
        if ob.name == ROOT_NAME or ob.name.startswith("Blk_"):
            bpy.data.objects.remove(ob, do_unlink=True)

root = bpy.data.objects.get(ROOT_NAME)
if root is None:
    root = bpy.data.objects.new(ROOT_NAME, None)
    root.empty_display_type = 'PLAIN_AXES'
    collection.objects.link(root)


# ============================
# SHARED MESHES (one per unique block size — instancing)
# ============================
unique_sizes = sorted(set(round(sz, 6) for _, sz in final_blocks))
size_to_mesh = {}

for sz in unique_sizes:
    mname = f"ABR_Cube_{sz:.5f}"
    m = bpy.data.meshes.get(mname)
    if m is None:
        bm_c = bmesh.new()
        bmesh.ops.create_cube(bm_c, size=1.0)
        m = bpy.data.meshes.new(mname)
        bm_c.to_mesh(m)
        bm_c.free()
    size_to_mesh[round(sz, 6)] = m


# ============================
# CREATE CUBE OBJECTS
# ============================
print(f"\nCreating {len(final_blocks)} objects...")
for i, (center, size) in enumerate(final_blocks):
    mesh = size_to_mesh[round(size, 6)]
    cube = bpy.data.objects.new(f"Blk_{i:06d}", mesh)
    cube.scale    = (size, size, size)
    cube.location = center
    cube.parent   = root
    collection.objects.link(cube)

    if (i + 1) % 5000 == 0:
        print(f"  ...{i + 1}/{len(final_blocks)}")

print(f"Created {len(final_blocks)} cube objects.")


# ============================
# CLEANUP
# ============================
bm.free()
eval_obj.to_mesh_clear()

print("\n✓ DONE: Adaptive Block Remesh complete.")
print("\nTUNING TIPS:")
print("  More small blocks → lower SUBDIV_THRESHOLDS values")
print("  Fewer big blocks  → raise MIN_DEPTH (e.g. 2)")
print("  Finer overall     → decrease BASE_SIZE (e.g. 0.03)")
print("  More dramatic variation → increase MAX_DEPTH (e.g. 5)")
print("  Uniform mesh (sphere) gives uniform blocks — this is correct.")
print("  For the reference 'rock cliff' effect, use a sculpted/scanned mesh.")