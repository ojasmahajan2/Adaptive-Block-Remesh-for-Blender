# Adaptive Block Remesh for Blender

**Octree-based adaptive block remeshing** — Generate a shell of separate cube objects that automatically vary in size based on local surface detail. Large blocks cover flat, simple areas while increasingly smaller, denser blocks appear on complex, detailed, or curved regions.

> Each cube is a separate object, so **Object Info → Random** works out of the box for materials and shading variation.

---

## ⚠️ AI Usage Notice

> [!IMPORTANT]
> **This project was developed with the assistance of AI tools.**
> 
> The core algorithm logic, code structure, and documentation were created with AI assistance. The code has been reviewed, tested, and iterated on within Blender to ensure correctness and usability. While AI was used as a development aid, all output has been verified for intended functionality by the author.
>
> Users are encouraged to review the code, report issues, and suggest improvements.

---

## Features

- **Adaptive Density** — Automatically uses large cubes on flat surfaces and tiny cubes on detailed/curved areas
- **Octree Subdivision** — Clean 2:1 size transitions with gap-free shell coverage
- **Dual Detail Detection** — Combines polygon density and surface curvature signals for accurate detail mapping
- **Per-Depth Thresholds** — Fine-grained control over subdivision aggressiveness at each octree level
- **Instanced Meshes** — Shared mesh data per unique block size for memory efficiency
- **Configurable Shell** — Control how tightly blocks conform to the surface
- **Safety Limits** — Built-in max block cap to prevent runaway generation

---

## Two Ways to Use

This repository provides **two versions** of the same core algorithm:

| | `script.py` | `adaptive_block_remesh.py` |
|---|---|---|
| **Type** | Standalone script | Blender addon |
| **Usage** | Copy → Paste → Run | Install via Preferences |
| **Settings** | Edit variables in the script | Full UI panel with sliders |
| **Best for** | Quick one-off use, experimentation | Repeated use, iterative tweaking |
| **Blender Version** | 4.0+ | 4.0+ |

---

## Quick Start — `script.py` (Copy & Paste)

The script is designed for Blender users who want to **quickly copy, paste, and run** without needing to install anything as an addon.

### Steps

1. **Select** a mesh object in Blender
2. Open the **Scripting** workspace (or any Text Editor area)
3. Create a new text block and **paste** the entire contents of `script.py`
4. **Adjust the settings** at the top of the script (see [Parameters](#-parameters) below)
5. Click **Run Script** (or press `Alt + P`)
6. Wait for the console to print `✓ DONE: Adaptive Block Remesh complete.`

### Configuring the Script

All settings are defined as variables near the top of the file. Edit them directly before running:

```python
# --- Block sizes ---
BASE_SIZE  = 0.03    # Smallest cube size (world units)
MAX_DEPTH  = 4       # Subdivision depth (largest cube = BASE_SIZE × 2^MAX_DEPTH)
MIN_DEPTH  = 1       # Force minimum subdivision depth everywhere

# --- Shell ---
SHELL_THICKNESS = 0.8   # Shell depth in multiples of BASE_SIZE

# --- Detail detection ---
SAMPLE_RADIUS_MULT = 0.85  # Sampling radius relative to cell size

# Per-depth subdivision thresholds [0..1]:
SUBDIV_THRESHOLDS = {
    0: 0.01,   # Almost always subdivide from root
    1: 0.12,   # Easy to subdivide
    2: 0.28,   # Moderate detail triggers subdivision
    3: 0.45,   # Significant detail needed for finest blocks
}

# Signal weights (should sum to 1.0):
DENSITY_WEIGHT   = 0.5
CURVATURE_WEIGHT = 0.5
```

---

## Addon — `adaptive_block_remesh.py` (Installable)

A full Blender addon with a dedicated UI panel and customizable options — ideal for iterative workflows.

### Installation

1. Open Blender → **Edit** → **Preferences** → **Add-ons**
2. Click **Install...** and select `adaptive_block_remesh.py`
3. Enable the addon by checking the box next to **"Adaptive Block Remesh"**

### Usage

1. **Select** a mesh object
2. Open the **Sidebar** in the 3D Viewport (`N` key)
3. Navigate to the **Block Remesh** tab
4. Configure settings using the UI sliders and fields
5. Click **⬛ Run Adaptive Block Remesh**

### Addon UI Panel

The addon panel is organized into collapsible sections:

- **Block Sizing** — `Base Size`, `Max Depth`, `Min Depth`
- **Shell** — `Shell Thickness`
- **Detail Detection** — `Sample Radius`, `Density Weight`, `Curvature Weight`
- **Subdivision Thresholds** *(collapsible)* — Per-depth thresholds (Depth 0–5)
- **Advanced** *(collapsible)* — `Max Blocks`, `Apply Scale`, `Delete Previous`, `Collection Name`
- **Block Size Range** — Live preview of the smallest/largest block sizes and total levels

---

## Parameters

### Block Sizing

| Parameter | Default | Range | Description |
|---|---|---|---|
| **Base Size** | `0.03` | `0.005 – 1.0` | Smallest cube edge length in world units. Decrease for finer detail (slower). |
| **Max Depth** | `4` | `1 – 6` | Octree subdivision depth. Largest cube = `Base Size × 2^Max Depth`. Higher values create more size variation. |
| **Min Depth** | `1` | `0 – 5` | Force subdivision to at least this depth everywhere. `0` allows the biggest blocks; raise for denser, more uniform results. |

With default settings (`Base Size = 0.03`, `Max Depth = 4`), the block sizes are:
```
Depth 0 → 0.48   (largest)
Depth 1 → 0.24
Depth 2 → 0.12
Depth 3 → 0.06
Depth 4 → 0.03   (smallest)
```

### Shell

| Parameter | Default | Range | Description |
|---|---|---|---|
| **Shell Thickness** | `0.8` | `0.1 – 3.0` | How tightly blocks conform to the surface, in multiples of Base Size. Lower = tighter fit. |

### Detail Detection

| Parameter | Default | Range | Description |
|---|---|---|---|
| **Sample Radius** | `0.85` | `0.3 – 1.5` | Neighbourhood sampling radius relative to cell size. Lower = sharper detail boundaries. |
| **Density Weight** | `0.5` | `0.0 – 1.0` | Weight for polygon-density signal. Higher = more sensitive to face count variation. Best for sculpts/scans with uneven topology. |
| **Curvature Weight** | `0.5` | `0.0 – 1.0` | Weight for surface-curvature signal. Higher = more sensitive to normal angle variation. Best for curved or rough surfaces. |

### Subdivision Thresholds

Per-depth thresholds control how aggressively each octree level subdivides. A cell subdivides when its measured detail **≥ threshold**.

| Depth | Default | Guidance |
|---|---|---|
| 0 (Coarsest) | `0.01` | Almost always subdivide — keep very low |
| 1 | `0.12` | Low detail still splits |
| 2 | `0.28` | Moderate detail triggers subdivision |
| 3 | `0.45` | Significant detail needed |
| 4 | `0.60` | High detail needed *(addon only)* |
| 5 (Finest) | `0.75` | Very high detail needed *(addon only)* |

**Lower thresholds** → more aggressive subdivision → denser small blocks at that level.

### Advanced / Output

| Parameter | Default | Description |
|---|---|---|
| **Max Blocks** | `400,000` | Safety limit on total output blocks |
| **Apply Scale** | `True` | Apply object scale before processing |
| **Delete Previous** | `True` | Remove previous block remesh result before running |
| **Collection Name** | `"AdaptiveBlockRemesh"` | Name of the output collection |

---

## How It Works

```
┌─────────────────────────────────────────────┐
│  1. Cover mesh bounding box with coarse     │
│     grid cells (largest block size)         │
├─────────────────────────────────────────────┤
│  2. For each cell near the surface:         │
│     • Measure polygon density               │
│     • Measure surface curvature (normals)   │
│     • Combine into a detail score [0..1]    │
├─────────────────────────────────────────────┤
│  3. detail ≥ threshold → Subdivide into 8   │
│     detail <  threshold → Keep as one block │
├─────────────────────────────────────────────┤
│  4. Recurse until detail is low OR          │
│     minimum block size is reached           │
├─────────────────────────────────────────────┤
│  5. All leaf cells on the surface become    │
│     separate cube objects                   │
└─────────────────────────────────────────────┘
```

The algorithm uses two complementary signals to measure surface detail:

- **Polygon Density** — Compares the local face count against the global average. Areas with more polygons packed together (sculpted regions, scans) score higher.
- **Surface Curvature** — Measures the average angular deviation of face normals from their local mean. Curved, rough, or complex surfaces score higher.

These signals are weighted and combined into a single detail score, which is compared against per-depth thresholds to decide whether a cell should subdivide further or remain as a large block.

---

## Tuning Tips

| Goal | Action |
|---|---|
| **More small blocks on details** | Lower the `SUBDIV_THRESHOLDS` values |
| **Fewer large blocks overall** | Raise `MIN_DEPTH` (e.g., `2`) |
| **Finer detail everywhere** | Decrease `BASE_SIZE` (e.g., `0.02`) |
| **More dramatic size variation** | Increase `MAX_DEPTH` (e.g., `5`) |
| **Tighter shell around surface** | Decrease `SHELL_THICKNESS` (e.g., `0.5`) |
| **Sharper detail boundaries** | Decrease `SAMPLE_RADIUS` |
| **Prioritize curvature over density** | Set `CURVATURE_WEIGHT = 0.8`, `DENSITY_WEIGHT = 0.2` |

> **Note:** A uniform mesh (e.g., a UV sphere) will produce uniform blocks — this is correct behavior. For the stylized adaptive effect, use sculpted, scanned, or otherwise non-uniform meshes.

---

## Important Notes

- **Performance:** Processing time scales with mesh complexity and block count. Start with higher `BASE_SIZE` values for initial tests.
- **Memory:** Each block is a separate Blender object. Very high block counts (100k+) may slow down the viewport. Use `Max Blocks` as a safety cap.
- **Object Info Random:** Since each cube is a separate object, you can use `Object Info → Random` in shader nodes to assign random colors, textures, or material variations per block.
- **Modifiers:** The script evaluates the mesh with all modifiers applied (using the dependency graph), so subdivision surfaces, booleans, etc. are respected.

---

## Requirements

- **Blender 4.0** or newer
- A mesh object with faces (works best with sculpted or scanned meshes)

---

## License

This project is provided as-is for personal and commercial use. See the repository for specific license details.
