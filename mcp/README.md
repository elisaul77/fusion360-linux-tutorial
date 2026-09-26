# Fusion360MCP — drive Autodesk Fusion from an AI assistant (Linux / Wine)

An [MCP](https://modelcontextprotocol.io) server + Fusion add-in that let an MCP client (Claude Code, Claude Desktop…)
model in Fusion running under Wine, including a **`fusion_execute_python`** tool with the **full Fusion API**.
That puts Fusion on par with the FreeCAD MCP bridges.

```
MCP client ──stdio──> server/server.py (Linux host) ──HTTP 127.0.0.1:7776──> Fusion360MCP add-in (inside Fusion/Wine)
                                                                              └─ runs every call on Fusion's main thread
```

> Tested September 2026 with Fusion 2702/2705 on the setup from the [main README](../README.md)
> (Docker + patched Wine + OpenGL). Should work with any Wine setup where Fusion runs.

## Tools

| Tool | What it does |
|---|---|
| `fusion_execute_python` | Run arbitrary Python inside Fusion. Preloaded: `adsk`, `app`, `ui`, `design`, `root`. Returns `print()` output and the variable `result`. Timeout 5–300 s |
| `fusion_get_design_info` | Sketches (with profile counts), bodies, components |
| `fusion_get_screenshot` | Viewport image returned to the client |
| `fusion_new_sketch`, `fusion_sketch_{line,circle,rectangle,polygon,arc}` | Sketches on XY / XZ / YZ |
| `fusion_extrude`, `fusion_revolve`, `fusion_fillet`, `fusion_chamfer`, `fusion_shell`, `fusion_mirror`, `fusion_circular_pattern`, `fusion_thread` | Basic features |
| `fusion_set_visual_style`, `fusion_undo`, `fusion_export_stl`, `fusion_export_step` | Misc |

Units in the API are **centimetres**. Fusion is **Y-up**: sketch on **XZ** for parts that stand on the ground.

## Security — read this

`execute_python` means *code execution on your machine*. A plain `127.0.0.1` port is not enough:
any web page open in your browser can POST to localhost. So:

- The add-in accepts a request only with header **`X-MCP-Token`** matching a shared secret.
  A web page cannot add a custom header cross-origin without a CORS preflight, which this server never grants.
- Any request carrying an **`Origin`** header (i.e. from a browser) gets **403**.
- No token configured → `execute_python` is **disabled** (the other tools still work).

The secret lives in `~/.autodesk_fusion/mcp_token` (mode 600). [`bin/fusion360`](../bin/fusion360) creates it
and passes it to Fusion as `FUSION_MCP_TOKEN`. The server reads the same file.
Override the folder with `FUSION_MCP_DIR` (set it for both the launcher and the MCP server).

## Install

1. **Add-in:** copy `addin/Fusion360MCP/` to
   `drive_c/users/<you>/AppData/Roaming/Autodesk/Autodesk Fusion 360/API/AddIns/`.
   In Fusion: **Shift+S → Fusion360MCP → Run** (tick *Run on startup*).
2. **Server** (on the Linux host): `pip install -r server/requirements.txt`, then register it, e.g. with Claude Code:
   ```bash
   claude mcp add fusion360 -- python3 /path/to/mcp/server/server.py
   ```
3. Start Fusion with `bin/fusion360` so the token exists. In Claude Code, run `/mcp` → *Reconnect* after changing the server.

## Lessons learned (Wine specifics)

- **Wine does not forward `$HOME`** to Windows programs. The add-in finds the Linux home via `WINEHOMEDIR`
  (`\??\Z:\home\<user>`) and writes shared files through `Z:\` (which maps to `/`).
- **Don't use `os.path.join` for Linux paths** inside Fusion's Python: it runs as Windows and inserts backslashes.
- **Sketch API points are in sketch space**, not world space. Build world points and convert with
  `sketch.modelToSketchSpace()`, or XZ/YZ sketches end up on the wrong plane.
- **Never `ui.messageBox()` on startup**: it is modal and blocks the main thread, so every MCP call times out.
- **Pair responses with request ids.** A request that times out still delivers its result later; without ids,
  the *next* request receives it.
- **Modal dialogs block everything** ("Recovered documents" at startup, the Add-ins dialog). Close them.
- If Fusion is killed while the add-in runs, Fusion **skips it on the next start** → re-enable it in Shift+S.

## Example

A parametric cup: user parameters, a driven diameter dimension, and the shell's open face chosen by geometry
instead of a guessed index:

```python
P = design.userParameters
for n, v in (('diametro', '70 mm'), ('altura', '95 mm'), ('pared', '2.4 mm')):
    P.add(n, adsk.core.ValueInput.createByString(v), 'mm', '')

sk = root.sketches.add(root.xZConstructionPlane)
c = sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(0, 0, 0), 3.5)
sk.sketchDimensions.addDiameterDimension(c, adsk.core.Point3D.create(4, 4, 0)).parameter.expression = 'diametro'

ext = root.features.extrudeFeatures.createInput(sk.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext.setDistanceExtent(False, adsk.core.ValueInput.createByString('altura'))
body = root.features.extrudeFeatures.add(ext).bodies.item(0)

top = max((f for f in body.faces if f.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType),
          key=lambda f: f.pointOnFace.y)
faces = adsk.core.ObjectCollection.create(); faces.add(top)
sh = root.features.shellFeatures.createInput(faces, False)
sh.insideThickness = adsk.core.ValueInput.createByString('pared')
root.features.shellFeatures.add(sh)
result = round(body.volume, 2)   # cm³ — change a parameter and the whole timeline recomputes
```

With the same tool we modelled a solitaire ring with a 57-facet round brilliant (real proportions:
57 % table, 34.5° crown, 40.75° pavilion). The facets are cut one by one with `TemporaryBRepManager` half-spaces
and added through a `baseFeature`.
