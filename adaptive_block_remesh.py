bl_info = {
    "name": "Adaptive Block Remesh",
    "author": "Custom",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Block Remesh",
    "description": "Adaptive octree-based block remesh with variable density. "
                   "Creates separate cube objects sized by local surface detail.",
    "category": "Mesh",
}

import bpy
import bmesh
import math
from mathutils import Vector, kdtree
from collections import defaultdict
from bpy.props import (
    FloatProperty,
    IntProperty,
    FloatVectorProperty,
    BoolProperty,
)


# ============================================================
# SCENE PROPERTIES (exposed in UI panel)
# ============================================================
class ABR_Properties(bpy.types.PropertyGroup):

    base_size: FloatProperty(
        name="Base Size",
        description="Smallest cube edge length in world units. "
                    "Decrease for finer detail (slower)",
        default=0.03,
        min=0.005, max=1.0,
        precision=3,
        step=1,
        subtype='DISTANCE',
    )
    max_depth: IntProperty(
        name="Max Depth",
        description="Octree subdivision depth. "
                    "Largest cube = Base Size × 2^Depth. "
                    "Higher = more size variation",
        default=4,
        min=1, max=6,
    )
    min_depth: IntProperty(
        name="Min Depth",
        description="Force subdivision to at least this depth everywhere. "
                    "0 = allow biggest blocks, raise for denser results",
        default=1,
        min=0, max=5,
    )
    shell_thickness: FloatProperty(
        name="Shell Thickness",
        description="How tightly blocks conform to the surface. "
                    "In multiples of Base Size. Lower = tighter fit",
        default=0.8,
        min=0.1, max=3.0,
        precision=2,
        step=10,
    )
    sample_radius: FloatProperty(
        name="Sample Radius",
        description="Neighbourhood sampling radius relative to cell size. "
                    "Lower = sharper detail boundaries",
        default=0.85,
        min=0.3, max=1.5,
        precision=2,
        step=5,
    )
    density_weight: FloatProperty(
        name="Density Weight",
        description="Weight for polygon-density signal (0..1). "
                    "Higher = more sensitive to polygon count variation",
        default=0.5,
        min=0.0, max=1.0,
        precision=2,
        step=5,
    )
    curvature_weight: FloatProperty(
        name="Curvature Weight",
        description="Weight for surface-curvature signal (0..1). "
                    "Higher = more sensitive to normal angle variation",
        default=0.5,
        min=0.0, max=1.0,
        precision=2,
        step=5,
    )

    # Per-depth thresholds
    thresh_0: FloatProperty(
        name="Threshold Depth 0",
        description="Detail threshold at depth 0 (coarsest). "
                    "Almost always subdivide — keep very low",
        default=0.01, min=0.0, max=1.0, precision=3, step=1,
    )
    thresh_1: FloatProperty(
        name="Threshold Depth 1",
        description="Detail threshold at depth 1",
        default=0.12, min=0.0, max=1.0, precision=2, step=1,
    )
    thresh_2: FloatProperty(
        name="Threshold Depth 2",
        description="Detail threshold at depth 2",
        default=0.28, min=0.0, max=1.0, precision=2, step=1,
    )
    thresh_3: FloatProperty(
        name="Threshold Depth 3",
        description="Detail threshold at depth 3. "
                    "Higher = only very detailed areas reach finest blocks",
        default=0.45, min=0.0, max=1.0, precision=2, step=1,
    )
    thresh_4: FloatProperty(
        name="Threshold Depth 4",
        description="Detail threshold at depth 4",
        default=0.60, min=0.0, max=1.0, precision=2, step=1,
    )
    thresh_5: FloatProperty(
        name="Threshold Depth 5",
        description="Detail threshold at depth 5 (finest)",
        default=0.75, min=0.0, max=1.0, precision=2, step=1,
    )

    max_blocks: IntProperty(
        name="Max Blocks",
        description="Safety limit on total output blocks",
        default=400000,
        min=1000, max=2000000,
    )
    apply_scale: BoolProperty(
        name="Apply Scale",
        description="Apply object scale before processing",
        default=True,
    )
    delete_previous: BoolProperty(
        name="Delete Previous",
        description="Remove previous Adaptive Block Remesh result before running",
        default=True,
    )
    collection_name: bpy.props.StringProperty(
        name="Collection",
        description="Name of the output collection",
        default="AdaptiveBlockRemesh",
    )

    # UI state
    show_thresholds: BoolProperty(
        name="Show Thresholds",
        description="Expand per-depth threshold settings",
        default=False,
    )
    show_advanced: BoolProperty(
        name="Show Advanced",
        description="Expand advanced settings",
        default=False,
    )


# ============================================================
# MAIN OPERATOR
# ============================================================
class ABR_OT_Run(bpy.types.Operator):
    bl_idname = "mesh.adaptive_block_remesh"
    bl_label = "Adaptive Block Remesh"
    bl_description = "Run adaptive octree block remesh on the active mesh object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        props = context.scene.abr_props
        obj   = context.active_object

        # Gather settings from UI properties
        BASE_SIZE         = props.base_size
        MAX_DEPTH         = props.max_depth
        MIN_DEPTH         = min(props.min_depth, MAX_DEPTH - 1)
        SHELL_THICKNESS   = props.shell_thickness
        SAMPLE_RADIUS_MULT = props.sample_radius
        DENSITY_WEIGHT    = props.density_weight
        CURVATURE_WEIGHT  = props.curvature_weight
        MAX_BLOCKS        = props.max_blocks
        APPLY_SCALE       = props.apply_scale
        DELETE_PREVIOUS   = props.delete_previous
        COLLECTION_NAME   = props.collection_name
        ROOT_NAME         = COLLECTION_NAME + "_ROOT"

        SUBDIV_THRESHOLDS = {
            0: props.thresh_0,
            1: props.thresh_1,
            2: props.thresh_2,
            3: props.thresh_3,
            4: props.thresh_4,
            5: props.thresh_5,
        }

        # Normalise weights
        wt = DENSITY_WEIGHT + CURVATURE_WEIGHT
        if wt > 0:
            DENSITY_WEIGHT   /= wt
            CURVATURE_WEIGHT /= wt
        else:
            DENSITY_WEIGHT = CURVATURE_WEIGHT = 0.5

        # ---- PREP ----
        bpy.ops.object.mode_set(mode='OBJECT')

        if APPLY_SCALE:
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        depsgraph = context.evaluated_depsgraph_get()
        eval_obj  = obj.evaluated_get(depsgraph)
        eval_mesh = eval_obj.to_mesh()

        world     = obj.matrix_world
        world_inv = world.inverted()
        world3    = world.to_3x3()

        # ---- BMESH ----
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
            self.report({'ERROR'}, "Mesh has no faces.")
            return {'CANCELLED'}

        num_faces = len(bm.faces)

        # ---- FACE DATA (world space) ----
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

        kd = kdtree.KDTree(len(face_centers_w))
        for i, co in enumerate(face_centers_w):
            kd.insert(co, i)
        kd.balance()

        # Global density
        bbox_world = [world @ Vector(c) for c in obj.bound_box]
        bbox_min = Vector((min(v.x for v in bbox_world),
                           min(v.y for v in bbox_world),
                           min(v.z for v in bbox_world)))
        bbox_max = Vector((max(v.x for v in bbox_world),
                           max(v.y for v in bbox_world),
                           max(v.z for v in bbox_world)))

        total_surface_area  = sum(face_areas)
        global_face_density = num_faces / max(total_surface_area, 1e-12)

        # ---- HELPERS ----
        def closest_surface(world_point):
            local_p = world_inv @ world_point
            ok, loc, normal, _ = eval_obj.closest_point_on_mesh(local_p)
            if not ok:
                return False, None, 1e9
            world_loc = world @ loc
            dist = (world_point - world_loc).length
            return True, world_loc, dist

        def measure_detail(cell_center, cell_size):
            radius = cell_size * SAMPLE_RADIUS_MULT
            hits = kd.find_range(cell_center, radius)
            if len(hits) < 3:
                return 0.0

            search_area    = math.pi * radius * radius
            expected_faces = global_face_density * search_area
            actual_faces   = len(hits)
            density_ratio  = actual_faces / max(expected_faces, 1.0)
            density_signal = max(0.0, min(1.0, (density_ratio - 0.3) / 2.5))

            normals = [face_normals_w[idx] for _, idx, _ in hits]
            avg_n = Vector((0.0, 0.0, 0.0))
            for n in normals:
                avg_n += n
            avg_n /= len(normals)
            if avg_n.length > 1e-8:
                avg_n.normalize()
            else:
                return 1.0

            angle_sum = 0.0
            for n in normals:
                d = max(-1.0, min(1.0, n.dot(avg_n)))
                angle_sum += math.acos(d)
            avg_angle = angle_sum / len(normals)
            curvature_signal = min(avg_angle / (math.pi / 3.0), 1.0)

            return density_signal * DENSITY_WEIGHT + curvature_signal * CURVATURE_WEIGHT

        # ---- OCTREE ----
        coarse_size = BASE_SIZE * (2 ** MAX_DEPTH)
        shell_dist  = BASE_SIZE * SHELL_THICKNESS
        HALF_SQRT3  = math.sqrt(3.0) / 2.0

        pad  = coarse_size
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

        # ---- TRAVERSAL ----
        final_blocks = []

        while stack:
            cell_center, cell_size, depth = stack.pop()

            if len(final_blocks) >= MAX_BLOCKS:
                break

            ok, surf_p, dist = closest_surface(cell_center)
            traverse_reach = cell_size * HALF_SQRT3 + shell_dist
            if not ok or dist > traverse_reach:
                continue

            # MAX_DEPTH leaf
            if depth >= MAX_DEPTH:
                if dist <= cell_size * 0.52 + shell_dist:
                    final_blocks.append((cell_center, cell_size))
                continue

            # Force subdivide below MIN_DEPTH
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

            # Detail-driven decision
            detail    = measure_detail(cell_center, cell_size)
            threshold = SUBDIV_THRESHOLDS.get(depth, 0.5)

            if detail >= threshold:
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
                if dist <= cell_size * 0.52 + shell_dist:
                    final_blocks.append((cell_center, cell_size))

        # ---- SHELL FILTER ----
        filtered = []
        for center, size in final_blocks:
            ok, surf_p, dist = closest_surface(center)
            if not ok:
                continue
            max_dist = size * 0.52 + shell_dist * 0.4
            if dist <= max_dist:
                filtered.append((center, size))

        final_blocks = filtered

        if not final_blocks:
            bm.free()
            eval_obj.to_mesh_clear()
            self.report({'WARNING'}, "No blocks generated. Try increasing Shell Thickness or reducing Base Size.")
            return {'CANCELLED'}

        # ---- COLLECTION SETUP ----
        scene      = context.scene
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

        # ---- SHARED MESHES ----
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

        # ---- CREATE OBJECTS ----
        for i, (center, size) in enumerate(final_blocks):
            mesh = size_to_mesh[round(size, 6)]
            cube = bpy.data.objects.new(f"Blk_{i:06d}", mesh)
            cube.scale    = (size, size, size)
            cube.location = center
            cube.parent   = root
            collection.objects.link(cube)

        # ---- CLEANUP ----
        bm.free()
        eval_obj.to_mesh_clear()

        # ---- REPORT ----
        sc = defaultdict(int)
        for _, sz in final_blocks:
            sc[round(sz, 6)] += 1

        size_info = ", ".join(
            f"{sz:.3f}×{cnt}" for sz, cnt in sorted(sc.items(), reverse=True)
        )
        self.report({'INFO'}, f"Created {len(final_blocks)} blocks  ({size_info})")
        return {'FINISHED'}


# ============================================================
# UI PANEL  (3D Viewport → Sidebar → Block Remesh tab)
# ============================================================
class ABR_PT_MainPanel(bpy.types.Panel):
    bl_label       = "Adaptive Block Remesh"
    bl_idname      = "ABR_PT_main"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Block Remesh"

    def draw(self, context):
        layout = context.scene.abr_props
        props  = context.scene.abr_props
        lay    = self.layout

        # Status
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            lay.label(text="Select a mesh object", icon='ERROR')
            return

        lay.label(text=f"Target: {obj.name}", icon='MESH_DATA')

        # Main settings
        box = lay.box()
        box.label(text="Block Sizing", icon='MOD_REMESH')
        col = box.column(align=True)
        col.prop(props, "base_size")
        col.prop(props, "max_depth")
        col.prop(props, "min_depth")

        # Shell
        box = lay.box()
        box.label(text="Shell", icon='MOD_SOLIDIFY')
        box.prop(props, "shell_thickness")

        # Detail detection
        box = lay.box()
        box.label(text="Detail Detection", icon='VIEWZOOM')
        col = box.column(align=True)
        col.prop(props, "sample_radius", text="Sample Radius")

        row = col.row(align=True)
        row.prop(props, "density_weight", text="Density")
        row.prop(props, "curvature_weight", text="Curvature")

        # Thresholds (collapsible)
        box = lay.box()
        row = box.row()
        row.prop(props, "show_thresholds",
                 icon='TRIA_DOWN' if props.show_thresholds else 'TRIA_RIGHT',
                 text="Subdivision Thresholds", emboss=False)
        if props.show_thresholds:
            col = box.column(align=True)
            col.prop(props, "thresh_0", text="Depth 0 (Coarsest)")
            col.prop(props, "thresh_1", text="Depth 1")
            col.prop(props, "thresh_2", text="Depth 2")
            col.prop(props, "thresh_3", text="Depth 3")
            if props.max_depth >= 5:
                col.prop(props, "thresh_4", text="Depth 4")
            if props.max_depth >= 6:
                col.prop(props, "thresh_5", text="Depth 5")

        # Advanced (collapsible)
        box = lay.box()
        row = box.row()
        row.prop(props, "show_advanced",
                 icon='TRIA_DOWN' if props.show_advanced else 'TRIA_RIGHT',
                 text="Advanced", emboss=False)
        if props.show_advanced:
            col = box.column(align=True)
            col.prop(props, "max_blocks")
            col.prop(props, "apply_scale")
            col.prop(props, "delete_previous")
            col.prop(props, "collection_name")

        # Size preview
        lay.separator()
        box = lay.box()
        box.label(text="Block Size Range:", icon='INFO')
        smallest = props.base_size
        largest  = props.base_size * (2 ** props.max_depth)
        box.label(text=f"  Smallest: {smallest:.4f}")
        box.label(text=f"  Largest:  {largest:.4f}")
        box.label(text=f"  Levels:   {props.max_depth + 1}")

        # Run button
        lay.separator()
        row = lay.row(align=True)
        row.scale_y = 1.8
        row.operator("mesh.adaptive_block_remesh",
                     text="⬛  Run Adaptive Block Remesh",
                     icon='PLAY')


# ============================================================
# REGISTER / UNREGISTER
# ============================================================
classes = (
    ABR_Properties,
    ABR_OT_Run,
    ABR_PT_MainPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.abr_props = bpy.props.PointerProperty(type=ABR_Properties)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.abr_props

if __name__ == "__main__":
    register()
