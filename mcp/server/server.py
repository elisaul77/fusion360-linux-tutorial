#!/usr/bin/env python3
"""
MCP server that bridges Claude Code to Fusion 360.

Runs on the Linux host as a stdio MCP server.
Forwards tool calls to the Fusion360MCP add-in via HTTP on localhost:7776.
"""

import asyncio
import base64
import json
import os
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

ADDIN_URL = "http://127.0.0.1:7776"
TOKEN_FILE = os.path.join(os.environ.get("FUSION_MCP_DIR") or os.path.expanduser("~/.autodesk_fusion"), "mcp_token")


def _token() -> str:
    """Shared secret also handed to Fusion by the launcher (see Fusion360MCP.py)."""
    tok = os.environ.get("FUSION_MCP_TOKEN", "").strip()
    if not tok:
        try:
            with open(TOKEN_FILE) as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
    return tok

server = Server("fusion360-mcp")


async def call_fusion(command: str, params: dict | None = None) -> dict:
    try:
        params = params or {}
        wait = min(max(int(params.get("timeout", 30)), 5), 300)
        async with httpx.AsyncClient(timeout=wait + 10.0) as client:
            resp = await client.post(
                f"{ADDIN_URL}/command",
                json={"command": command, "params": params},
                headers={"X-MCP-Token": _token()},
            )
            return resp.json()
    except httpx.ConnectError:
        return {
            "error": (
                "Cannot connect to Fusion 360 MCP add-in.\n"
                "Make sure:\n"
                "  1. Fusion 360 is open\n"
                "  2. Fusion360MCP add-in is running:\n"
                "     Tools → Add-Ins (Shift+S) → Fusion360MCP → Run"
            )
        }
    except Exception as e:
        return {"error": str(e)}


def to_text(result: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # --- Info & screenshot ---
        types.Tool(
            name="fusion_get_design_info",
            description=(
                "Get info about the current Fusion 360 design: "
                "sketches (with profile counts), 3D bodies, and components. "
                "Always call this first to know the current state."
            ),
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="fusion_get_screenshot",
            description=(
                "Capture the current Fusion 360 viewport as an image and return it here. "
                "Call this after creating or modifying geometry to see the result. "
                "The viewport auto-fits before capturing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "default": 1280, "description": "Image width in pixels"},
                    "height": {"type": "integer", "default": 720, "description": "Image height in pixels"}
                }
            }
        ),

        # --- Sketches ---
        types.Tool(
            name="fusion_new_sketch",
            description="Create a new sketch on a construction plane. Returns sketch index.",
            inputSchema={
                "type": "object",
                "required": ["plane"],
                "properties": {
                    "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
                    "name": {"type": "string"}
                }
            }
        ),
        types.Tool(
            name="fusion_sketch_circle",
            description="Draw a circle in a sketch. Units: centimeters (cm).",
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "cx", "cy", "radius"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "cx": {"type": "number"}, "cy": {"type": "number"},
                    "radius": {"type": "number"}
                }
            }
        ),
        types.Tool(
            name="fusion_sketch_rectangle",
            description="Draw a rectangle by two opposite corners. Units: cm.",
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "x1", "y1", "x2", "y2"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "x1": {"type": "number"}, "y1": {"type": "number"},
                    "x2": {"type": "number"}, "y2": {"type": "number"}
                }
            }
        ),
        types.Tool(
            name="fusion_sketch_line",
            description="Draw a line in a sketch. Units: cm.",
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "x1", "y1", "x2", "y2"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "x1": {"type": "number"}, "y1": {"type": "number"},
                    "x2": {"type": "number"}, "y2": {"type": "number"}
                }
            }
        ),
        types.Tool(
            name="fusion_sketch_arc",
            description="Draw an arc in a sketch by center, radius, and angle range. Units: cm / degrees.",
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "cx", "cy", "radius", "start_angle", "end_angle"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "cx": {"type": "number"}, "cy": {"type": "number"},
                    "radius": {"type": "number"},
                    "start_angle": {"type": "number", "description": "Start angle in degrees"},
                    "end_angle": {"type": "number", "description": "End angle in degrees"}
                }
            }
        ),
        types.Tool(
            name="fusion_sketch_polygon",
            description="Draw a regular polygon (3–12 sides) inscribed in a circle. Units: cm.",
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "sides", "cx", "cy", "radius"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "sides": {"type": "integer", "minimum": 3, "maximum": 12},
                    "cx": {"type": "number"}, "cy": {"type": "number"},
                    "radius": {"type": "number"}
                }
            }
        ),

        # --- 3D features ---
        types.Tool(
            name="fusion_extrude",
            description=(
                "Extrude a closed sketch profile into a 3D body. Units: cm. "
                "Use fusion_get_design_info to find sketch_index and profile_index."
            ),
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "distance"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "profile_index": {"type": "integer", "default": 0},
                    "distance": {"type": "number", "description": "Height in cm"},
                    "operation": {"type": "string", "enum": ["NewBody", "Join", "Cut", "Intersect"], "default": "NewBody"}
                }
            }
        ),
        types.Tool(
            name="fusion_revolve",
            description=(
                "Revolve a sketch profile around a construction axis to create a 3D body. "
                "Ideal for axially-symmetric parts: shafts, bolts, cups, knobs. Units: cm / degrees."
            ),
            inputSchema={
                "type": "object",
                "required": ["sketch_index", "axis"],
                "properties": {
                    "sketch_index": {"type": "integer"},
                    "profile_index": {"type": "integer", "default": 0},
                    "axis": {"type": "string", "enum": ["X", "Y", "Z"], "description": "Axis of revolution"},
                    "angle": {"type": "number", "default": 360, "description": "Sweep angle in degrees (360 = full revolution)"},
                    "operation": {"type": "string", "enum": ["NewBody", "Join", "Cut"], "default": "NewBody"}
                }
            }
        ),
        types.Tool(
            name="fusion_fillet",
            description=(
                "Round all edges of a body with a constant radius fillet. "
                "Essential for engineering parts — removes sharp edges, improves aesthetics and strength."
            ),
            inputSchema={
                "type": "object",
                "required": ["body_index", "radius"],
                "properties": {
                    "body_index": {"type": "integer"},
                    "radius": {"type": "number", "description": "Fillet radius in cm"}
                }
            }
        ),
        types.Tool(
            name="fusion_chamfer",
            description="Bevel all edges of a body at 45°. Common in machined parts and thread lead-ins.",
            inputSchema={
                "type": "object",
                "required": ["body_index", "distance"],
                "properties": {
                    "body_index": {"type": "integer"},
                    "distance": {"type": "number", "description": "Chamfer distance in cm"}
                }
            }
        ),
        types.Tool(
            name="fusion_shell",
            description=(
                "Hollow out a solid body by removing material from the inside, keeping a thin wall. "
                "Great for enclosures, containers, and lightweight parts."
            ),
            inputSchema={
                "type": "object",
                "required": ["body_index", "thickness"],
                "properties": {
                    "body_index": {"type": "integer"},
                    "thickness": {"type": "number", "description": "Wall thickness in cm"},
                    "open_face_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "default": [0],
                        "description": "Face indices to remove (open faces of the shell)"
                    }
                }
            }
        ),
        types.Tool(
            name="fusion_mirror",
            description="Mirror a body across a construction plane to create a symmetric copy.",
            inputSchema={
                "type": "object",
                "required": ["body_index", "plane"],
                "properties": {
                    "body_index": {"type": "integer"},
                    "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]}
                }
            }
        ),
        types.Tool(
            name="fusion_circular_pattern",
            description=(
                "Create a circular array of a body around a construction axis. "
                "Perfect for bolt hole patterns, fan blades, gear teeth, spoke wheels."
            ),
            inputSchema={
                "type": "object",
                "required": ["body_index", "count", "axis"],
                "properties": {
                    "body_index": {"type": "integer"},
                    "count": {"type": "integer", "description": "Number of copies (including original)"},
                    "axis": {"type": "string", "enum": ["X", "Y", "Z"]}
                }
            }
        ),

        types.Tool(
            name="fusion_thread",
            description=(
                "Apply a standard helical thread to a cylindrical face using Fusion 360's "
                "native ThreadFeature. Creates real 3D geometry (modeled=true) or cosmetic "
                "threads. For M14 bolt use designation='M14x2', radius=0.7, length=3.7."
            ),
            inputSchema={
                "type": "object",
                "required": ["body_index", "radius"],
                "properties": {
                    "body_index":    {"type": "integer", "description": "Index of the body to thread"},
                    "radius":        {"type": "number",  "description": "Nominal radius of the cylinder in cm"},
                    "length":        {"type": "number",  "default": 3.7, "description": "Thread length in cm"},
                    "designation":   {"type": "string",  "default": "M14x2", "description": "Thread designation, e.g. M14x2, M8x1.25"},
                    "thread_type":   {"type": "string",  "default": "ISO Metric Profile"},
                    "thread_class":  {"type": "string",  "default": "6g", "description": "6g=external standard, 6H=internal"},
                    "full_length":   {"type": "boolean", "default": False},
                    "modeled":       {"type": "boolean", "default": True, "description": "True=3D geometry, False=cosmetic only"},
                    "offset":        {"type": "number",  "default": 0.0,  "description": "Offset from face edge in cm"}
                }
            }
        ),

        # --- Export ---
        types.Tool(
            name="fusion_export_stl",
            description="Export the design as STL to a Linux path.",
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}}
            }
        ),
        types.Tool(
            name="fusion_export_step",
            description="Export the design as STEP to a Linux path.",
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}}
            }
        ),

        # --- Misc ---
        types.Tool(
            name="fusion_execute_python",
            description=(
                "Run arbitrary Python inside Fusion 360 on its main thread, with the full Fusion API. "
                "Preloaded names: adsk, app, ui, design (active adsk.fusion.Design or None), root "
                "(design.rootComponent). Units are cm. print() output is returned; set a variable "
                "named `result` to return a value. Use it for anything the fixed tools cannot do "
                "(parameters, components, joints, sketch constraints, inspecting faces/edges...)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to exec()"},
                    "timeout": {"type": "integer", "description": "Seconds to wait (5-300, default 30)"},
                },
                "required": ["code"]
            }
        ),
        types.Tool(
            name="fusion_set_visual_style",
            description="Set the viewport visual style (shaded, shaded_edges, shaded_hidden_edges, wireframe, wireframe_hidden_edges, wireframe_visible_edges).",
            inputSchema={
                "type": "object",
                "properties": {"style": {"type": "string", "enum": [
                    "shaded", "shaded_edges", "shaded_hidden_edges",
                    "wireframe", "wireframe_hidden_edges", "wireframe_visible_edges"]}},
                "required": ["style"]
            }
        ),
        types.Tool(
            name="fusion_undo",
            description="Undo the last operation in Fusion 360.",
            inputSchema={"type": "object", "properties": {}}
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_TO_COMMAND = {
    "fusion_get_design_info":  "get_design_info",
    "fusion_get_screenshot":   "get_screenshot",
    "fusion_new_sketch":       "new_sketch",
    "fusion_sketch_circle":    "sketch_circle",
    "fusion_sketch_rectangle": "sketch_rectangle",
    "fusion_sketch_line":      "sketch_line",
    "fusion_sketch_arc":       "sketch_arc",
    "fusion_sketch_polygon":   "sketch_polygon",
    "fusion_extrude":          "extrude",
    "fusion_revolve":          "revolve",
    "fusion_fillet":           "fillet",
    "fusion_chamfer":          "chamfer",
    "fusion_shell":            "shell",
    "fusion_mirror":           "mirror",
    "fusion_circular_pattern": "circular_pattern",
    "fusion_thread":           "thread",
    "fusion_export_stl":       "export_stl",
    "fusion_export_step":      "export_step",
    "fusion_set_visual_style": "set_visual_style",
    "fusion_undo":             "undo",
    "fusion_execute_python":   "execute_python",
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent]:
    command = TOOL_TO_COMMAND.get(name)
    if command is None:
        return to_text({"error": f"Unknown tool '{name}'"})

    result = await call_fusion(command, arguments)

    # Screenshot: return as image so Claude can see the viewport
    if name == "fusion_get_screenshot":
        if "error" in result:
            return to_text(result)
        path = result.get("path", "")
        try:
            with open(path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode()
            return [types.ImageContent(type="image", data=image_data, mimeType="image/png")]
        except Exception as e:
            return to_text({"error": f"Could not read screenshot from {path}: {e}"})

    return to_text(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
