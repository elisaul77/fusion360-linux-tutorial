"""
Fusion360MCP — Add-in that exposes Fusion 360 API over a local HTTP server.

Architecture:
  HTTP thread (background) receives POST /command from the host MCP server.
  It fires a Fusion custom event to marshal the call onto Fusion's main thread.
  The main-thread handler executes the API call and puts the result in a queue.
  The HTTP thread retrieves the result from the queue and returns it as JSON.

All Fusion API calls MUST run on the main thread — this pattern is required.
"""

import adsk.core
import adsk.fusion
import traceback
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import queue
import time
import os
import io
import hmac
import contextlib

# --- globals ---
_app = None
_ui = None
_httpd = None
_http_thread = None
_custom_event = None
_event_handler = None
_result_queue = queue.Queue()
_request_lock = threading.Lock()  # serialize concurrent HTTP requests
_req_counter = [0]  # request id, to discard results of requests that timed out

EVENT_ID = 'FusionMCPExecute'

PORT = 7776


def _host_home():
    """Linux home directory of the user running Wine.

    Wine does NOT forward $HOME to Windows programs, but it sets
    WINEHOMEDIR=\\??\\Z:\\home\\<user> (Z:\\ maps to /)."""
    w = os.environ.get('WINEHOMEDIR', '')
    if 'Z:' in w:
        return w.split('Z:', 1)[1].replace('\\', '/').rstrip('/') or '/'
    return os.environ.get('HOME', '/tmp').rstrip('/')


# Files shared with the host MCP server (token, screenshots): $FUSION_MCP_DIR or ~/.autodesk_fusion.
# Plain '/' joins on purpose: under Wine os.path.join would insert a backslash into a Linux path.
HOST_DIR = (os.environ.get('FUSION_MCP_DIR') or _host_home() + '/.autodesk_fusion').rstrip('/')


def _wine_path(linux_path):
    """Linux path -> path usable by Fusion under Wine (Z:\\ maps to /)."""
    return 'Z:' + linux_path.replace('/', '\\')


# ---------------------------------------------------------------------------
# Command implementations — all called on the Fusion main thread
# ---------------------------------------------------------------------------

def _get_design():
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError('No active Fusion 360 design open')
    return design


def cmd_get_design_info(_params):
    design = _get_design()
    root = design.rootComponent

    sketches = [
        {'index': i, 'name': root.sketches.item(i).name,
         'profiles': root.sketches.item(i).profiles.count}
        for i in range(root.sketches.count)
    ]
    bodies = [
        {'index': i, 'name': root.bRepBodies.item(i).name,
         'visible': root.bRepBodies.item(i).isVisible}
        for i in range(root.bRepBodies.count)
    ]
    components = [
        {'index': i, 'name': root.occurrences.item(i).name}
        for i in range(root.occurrences.count)
    ]
    return {
        'document': _app.activeDocument.name,
        'units': 'cm',
        'note': 'All geometry values are in centimeters (Fusion internal unit)',
        'sketches': sketches,
        'bodies': bodies,
        'components': components,
    }


def cmd_new_sketch(params):
    design = _get_design()
    root = design.rootComponent
    plane_name = params.get('plane', 'XY').upper()
    planes = {
        'XY': root.xYConstructionPlane,
        'XZ': root.xZConstructionPlane,
        'YZ': root.yZConstructionPlane,
    }
    if plane_name not in planes:
        raise ValueError(f"Unknown plane '{plane_name}'. Use XY, XZ, or YZ")
    sketch = root.sketches.add(planes[plane_name])
    if 'name' in params:
        sketch.name = params['name']
    idx = root.sketches.count - 1
    return {'index': idx, 'name': sketch.name, 'plane': plane_name}


def cmd_sketch_circle(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    center = _sketch_point(sketch, params.get('cx', 0.0), params.get('cy', 0.0))
    radius = params.get('radius', 1.0)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(center, radius)
    return {'ok': True, 'sketch': sketch.name, 'profiles': sketch.profiles.count}


def cmd_sketch_rectangle(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    p1 = _sketch_point(sketch, params.get('x1', 0.0), params.get('y1', 0.0))
    p2 = _sketch_point(sketch, params.get('x2', 1.0), params.get('y2', 1.0))
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
    return {'ok': True, 'sketch': sketch.name, 'profiles': sketch.profiles.count}


def cmd_sketch_line(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    p1 = _sketch_point(sketch, params.get('x1', 0.0), params.get('y1', 0.0))
    p2 = _sketch_point(sketch, params.get('x2', 1.0), params.get('y2', 0.0))
    sketch.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
    return {'ok': True, 'sketch': sketch.name}


def cmd_sketch_polygon(params):
    """Regular polygon inscribed in a circle."""
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    sides = int(params.get('sides', 6))
    cx = params.get('cx', 0.0)
    cy = params.get('cy', 0.0)
    radius = params.get('radius', 1.0)
    import math
    pts = []
    for i in range(sides):
        angle = 2 * math.pi * i / sides
        pts.append(_sketch_point(sketch,
                                 cx + radius * math.cos(angle),
                                 cy + radius * math.sin(angle)))
    lines = sketch.sketchCurves.sketchLines
    for i in range(sides):
        lines.addByTwoPoints(pts[i], pts[(i + 1) % sides])
    return {'ok': True, 'sketch': sketch.name, 'sides': sides, 'profiles': sketch.profiles.count}


def cmd_extrude(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    profile_idx = params.get('profile_index', 0)
    if profile_idx >= sketch.profiles.count:
        raise IndexError(f"Profile index {profile_idx} not found (sketch has {sketch.profiles.count})")
    profile = sketch.profiles.item(profile_idx)
    distance = params.get('distance', 1.0)
    op_map = {
        'NewBody':   adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        'Join':      adsk.fusion.FeatureOperations.JoinFeatureOperation,
        'Cut':       adsk.fusion.FeatureOperations.CutFeatureOperation,
        'Intersect': adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }
    operation = op_map.get(params.get('operation', 'NewBody'),
                           adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    dist_val = adsk.core.ValueInput.createByReal(distance)
    root.features.extrudeFeatures.addSimple(profile, dist_val, operation)
    return {'ok': True, 'bodies': root.bRepBodies.count}


def cmd_export_stl(params):
    design = _get_design()
    linux_path = params.get('path', '/tmp/fusion_export.stl')
    # Wine maps the host filesystem under Z:\ drive
    wine_path = 'Z:' + linux_path.replace('/', '\\')
    em = design.exportManager
    opts = em.createSTLExportOptions(design.rootComponent, wine_path)
    opts.sendToPrintUtility = False
    em.execute(opts)
    return {'ok': True, 'path': linux_path}


def cmd_export_step(params):
    design = _get_design()
    linux_path = params.get('path', '/tmp/fusion_export.step')
    wine_path = 'Z:' + linux_path.replace('/', '\\')
    em = design.exportManager
    opts = em.createSTEPExportOptions(wine_path)
    em.execute(opts)
    return {'ok': True, 'path': linux_path}


def cmd_get_screenshot(params):
    # Write through Z:\ (host filesystem) so it works regardless of the wine prefix
    linux_path = HOST_DIR + '/mcp_screenshot.png'
    wine_path = _wine_path(linux_path)
    width = int(params.get('width', 1280))
    height = int(params.get('height', 720))
    viewport = _app.activeViewport
    try:
        viewport.fit()
    except Exception:
        pass
    ok = viewport.saveAsImageFile(wine_path, width, height)
    if not ok:
        return {'error': 'saveAsImageFile returned False — is a design open?'}
    return {'ok': True, 'path': linux_path}


def cmd_revolve(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    profile_idx = params.get('profile_index', 0)
    if profile_idx >= sketch.profiles.count:
        raise IndexError(f"Profile index {profile_idx} not found")
    profile = sketch.profiles.item(profile_idx)

    axis_name = params.get('axis', 'Y').upper()
    axes = {
        'X': root.xConstructionAxis,
        'Y': root.yConstructionAxis,
        'Z': root.zConstructionAxis,
    }
    if axis_name not in axes:
        raise ValueError(f"Unknown axis '{axis_name}'. Use X, Y, or Z")

    import math
    angle_deg = params.get('angle', 360.0)
    angle_rad = math.radians(angle_deg)
    op_map = {
        'NewBody':   adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        'Join':      adsk.fusion.FeatureOperations.JoinFeatureOperation,
        'Cut':       adsk.fusion.FeatureOperations.CutFeatureOperation,
    }
    operation = op_map.get(params.get('operation', 'NewBody'),
                           adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

    revolves = root.features.revolveFeatures
    inp = revolves.createInput(profile, axes[axis_name], operation)
    inp.setAngleExtent(False, adsk.core.ValueInput.createByReal(angle_rad))
    revolves.add(inp)
    return {'ok': True, 'bodies': root.bRepBodies.count}


def cmd_fillet(params):
    design = _get_design()
    root = design.rootComponent
    radius = params.get('radius', 0.1)
    body_idx = params.get('body_index', 0)
    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")
    body = root.bRepBodies.item(body_idx)

    edges = adsk.core.ObjectCollection.create()
    for edge in body.edges:
        edges.add(edge)

    fillets = root.features.filletFeatures
    inp = fillets.createInput()
    inp.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal(radius), True)
    fillets.add(inp)
    return {'ok': True}


def cmd_chamfer(params):
    design = _get_design()
    root = design.rootComponent
    distance = params.get('distance', 0.1)
    body_idx = params.get('body_index', 0)
    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")
    body = root.bRepBodies.item(body_idx)

    edges = adsk.core.ObjectCollection.create()
    for edge in body.edges:
        edges.add(edge)

    chamfers = root.features.chamferFeatures
    inp = chamfers.createInput(edges, True)
    inp.setToEqualDistance(adsk.core.ValueInput.createByReal(distance))
    chamfers.add(inp)
    return {'ok': True}


def cmd_shell(params):
    design = _get_design()
    root = design.rootComponent
    thickness = params.get('thickness', 0.2)
    body_idx = params.get('body_index', 0)
    face_indices = params.get('open_face_indices', [0])

    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")
    body = root.bRepBodies.item(body_idx)

    faces = adsk.core.ObjectCollection.create()
    for i in face_indices:
        faces.add(body.faces.item(i))

    shells = root.features.shellFeatures
    inp = shells.createInput(faces, False)
    inp.insideThickness = adsk.core.ValueInput.createByReal(thickness)
    shells.add(inp)
    return {'ok': True}


def cmd_mirror(params):
    design = _get_design()
    root = design.rootComponent
    plane_name = params.get('plane', 'XZ').upper()
    planes = {
        'XY': root.xYConstructionPlane,
        'XZ': root.xZConstructionPlane,
        'YZ': root.yZConstructionPlane,
    }
    if plane_name not in planes:
        raise ValueError(f"Unknown plane '{plane_name}'. Use XY, XZ, or YZ")

    body_idx = params.get('body_index', 0)
    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")

    bodies = adsk.core.ObjectCollection.create()
    bodies.add(root.bRepBodies.item(body_idx))

    mirrors = root.features.mirrorFeatures
    inp = mirrors.createInput(bodies, planes[plane_name])
    mirrors.add(inp)
    return {'ok': True, 'bodies': root.bRepBodies.count}


def cmd_circular_pattern(params):
    design = _get_design()
    root = design.rootComponent
    body_idx = params.get('body_index', 0)
    count = int(params.get('count', 4))
    axis_name = params.get('axis', 'Z').upper()
    axes = {
        'X': root.xConstructionAxis,
        'Y': root.yConstructionAxis,
        'Z': root.zConstructionAxis,
    }
    if axis_name not in axes:
        raise ValueError(f"Unknown axis '{axis_name}'")
    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")

    import math
    bodies = adsk.core.ObjectCollection.create()
    bodies.add(root.bRepBodies.item(body_idx))

    patterns = root.features.circularPatternFeatures
    inp = patterns.createInput(bodies, axes[axis_name])
    inp.quantity = adsk.core.ValueInput.createByReal(count)
    inp.totalAngle = adsk.core.ValueInput.createByReal(math.radians(360))
    inp.isSymmetric = False
    patterns.add(inp)
    return {'ok': True, 'bodies': root.bRepBodies.count}


def cmd_sketch_arc(params):
    design = _get_design()
    root = design.rootComponent
    sketch = _get_sketch(root, params)
    cx = params.get('cx', 0.0)
    cy = params.get('cy', 0.0)
    r = params.get('radius', 1.0)
    import math
    start_angle = math.radians(params.get('start_angle', 0.0))
    sweep = math.radians(params.get('end_angle', 90.0) - params.get('start_angle', 0.0))
    center = _sketch_point(sketch, cx, cy)
    start_pt = _sketch_point(sketch,
                             cx + r * math.cos(start_angle),
                             cy + r * math.sin(start_angle))
    sketch.sketchCurves.sketchArcs.addByCenterStartSweep(
        center, start_pt, sweep
    )
    return {'ok': True, 'sketch': sketch.name}


def cmd_thread(params):
    """Apply Fusion 360 native helical thread to a cylindrical face."""
    design = _get_design()
    root = design.rootComponent

    body_idx = params.get('body_index', 0)
    if body_idx >= root.bRepBodies.count:
        raise IndexError(f"Body index {body_idx} not found")
    body = root.bRepBodies.item(body_idx)

    target_radius = params.get('radius', 0.7)   # cm — nominal radius of cylinder
    thread_length = params.get('length', 3.7)   # cm — how far thread extends
    thread_type   = params.get('thread_type', 'ISO Metric Profile')
    designation   = params.get('designation', 'M14x2')
    thread_class  = params.get('thread_class', '6g')   # 6g = standard external fit
    is_modeled    = params.get('modeled', True)         # True = real 3D geometry

    # Find cylindrical face closest to the requested radius
    best_face = None
    best_diff = 999
    for face in body.faces:
        geom = face.geometry
        if geom.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
            diff = abs(geom.radius - target_radius)
            if diff < best_diff:
                best_diff = diff
                best_face = face

    if best_face is None or best_diff > 0.1:
        return {'error': f'No cylindrical face found near radius {target_radius} cm. '
                         f'Closest diff: {best_diff:.4f} cm'}

    thread_feats = root.features.threadFeatures
    # createThreadInfo(isInternal, threadType, designation, threadClass)
    thread_info  = thread_feats.createThreadInfo(
        False, thread_type, designation, thread_class  # False = external
    )

    inp = thread_feats.createInput(best_face, thread_info)
    inp.isFullLength = params.get('full_length', False)
    inp.threadLength = adsk.core.ValueInput.createByReal(thread_length)
    inp.offset       = adsk.core.ValueInput.createByReal(params.get('offset', 0.0))
    if hasattr(inp, 'isModeled'):
        inp.isModeled = is_modeled

    thread_feats.add(inp)
    return {
        'ok': True,
        'designation': designation,
        'length_cm': thread_length,
        'modeled': is_modeled,
    }


def cmd_set_visual_style(params):
    styles = {
        'shaded': adsk.core.VisualStyles.ShadedVisualStyle,
        'shaded_edges': adsk.core.VisualStyles.ShadedWithVisibleEdgesOnlyVisualStyle,
        'shaded_hidden_edges': adsk.core.VisualStyles.ShadedWithHiddenEdgesVisualStyle,
        'wireframe': adsk.core.VisualStyles.WireframeVisualStyle,
        'wireframe_hidden_edges': adsk.core.VisualStyles.WireframeWithHiddenEdgesVisualStyle,
        'wireframe_visible_edges': adsk.core.VisualStyles.WireframeWithVisibleEdgesOnlyVisualStyle,
    }
    name = params.get('style', 'shaded_edges')
    if name not in styles:
        raise ValueError(f"Unknown style '{name}'. Available: {list(styles)}")
    _app.activeViewport.visualStyle = styles[name]
    return {'ok': True, 'style': name}


def cmd_undo(_params):
    design = _get_design()
    timeline = design.timeline
    pos = timeline.markerPosition
    if pos > 0:
        timeline.item(pos - 1).rollTo(True)
    return {'ok': True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sketch(root, params):
    idx = params.get('sketch_index', 0)
    if idx >= root.sketches.count:
        raise IndexError(f"Sketch index {idx} not found (design has {root.sketches.count})")
    return root.sketches.item(idx)


def _sketch_point(sketch, x, y):
    """Convert (x, y) in the sketch's local 2-D frame to a world-space Point3D.

    Our API exposes all sketches with the same (x, y) parameters, but the
    world coordinates depend on which construction plane the sketch is on:
      XY plane (normal ≈ Z): world = (x, y, 0)
      XZ plane (normal ≈ Y): world = (x, 0, y)
      YZ plane (normal ≈ X): world = (0, x, y)
    """
    n = sketch.referencePlane.geometry.normal
    if abs(n.y) > 0.9:      # XZ plane
        world = adsk.core.Point3D.create(x, 0.0, y)
    elif abs(n.x) > 0.9:    # YZ plane
        world = adsk.core.Point3D.create(0.0, x, y)
    else:                   # XY plane (default)
        world = adsk.core.Point3D.create(x, y, 0.0)
    # Sketch APIs (addByTwoPoints, addByCenterRadius...) take sketch-space points
    return sketch.modelToSketchSpace(world)


# ---------------------------------------------------------------------------
# Auth + arbitrary Python execution
# ---------------------------------------------------------------------------

# Shared secret created by the host launcher (umask 077) and passed in as an env var.
# Without it, execute_python stays disabled. Every POST must carry it in X-MCP-Token:
# a custom header cannot be sent cross-origin by a web page without a CORS preflight
# (which this server never grants), so a browser tab cannot drive Fusion.


def _load_token():
    tok = os.environ.get('FUSION_MCP_TOKEN', '').strip()
    if not tok:
        try:
            with open(_wine_path(HOST_DIR + '/mcp_token')) as f:
                tok = f.read().strip()
        except OSError:
            tok = ''
    return tok


_TOKEN = ''
MAX_OUTPUT = 20000


def cmd_execute_python(params):
    """Run arbitrary Python on Fusion's main thread with the API preloaded.

    Namespace: adsk, app, ui, design (active Design or None), root (its rootComponent or None).
    Returns stdout and repr() of a variable named `result`, if the script sets one.
    """
    if not _TOKEN:
        return {'error': 'execute_python is disabled: no FUSION_MCP_TOKEN configured'}
    code = params.get('code', '')
    if not code.strip():
        return {'error': 'empty code'}
    design = adsk.fusion.Design.cast(_app.activeProduct)
    ns = {'adsk': adsk, 'app': _app, 'ui': _ui, 'design': design,
          'root': design.rootComponent if design else None, '__name__': '__fusion_mcp__'}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, '<fusion_mcp>', 'exec'), ns)
    except Exception:
        return {'ok': False, 'stdout': out.getvalue()[-MAX_OUTPUT:], 'error': traceback.format_exc()}
    res = {'ok': True, 'stdout': out.getvalue()[-MAX_OUTPUT:]}
    if 'result' in ns:
        res['result'] = repr(ns['result'])[:MAX_OUTPUT]
    return res


COMMANDS = {
    'get_design_info':    cmd_get_design_info,
    'new_sketch':         cmd_new_sketch,
    'sketch_circle':      cmd_sketch_circle,
    'sketch_rectangle':   cmd_sketch_rectangle,
    'sketch_line':        cmd_sketch_line,
    'sketch_polygon':     cmd_sketch_polygon,
    'sketch_arc':         cmd_sketch_arc,
    'extrude':            cmd_extrude,
    'revolve':            cmd_revolve,
    'fillet':             cmd_fillet,
    'chamfer':            cmd_chamfer,
    'shell':              cmd_shell,
    'mirror':             cmd_mirror,
    'circular_pattern':   cmd_circular_pattern,
    'thread':             cmd_thread,
    'export_stl':         cmd_export_stl,
    'export_step':        cmd_export_step,
    'get_screenshot':     cmd_get_screenshot,
    'set_visual_style':   cmd_set_visual_style,
    'undo':               cmd_undo,
    'execute_python':     cmd_execute_python,
}


def execute_command(command, params):
    fn = COMMANDS.get(command)
    if fn is None:
        return {'error': f"Unknown command '{command}'. Available: {list(COMMANDS)}"}
    return fn(params)


# ---------------------------------------------------------------------------
# HTTP server (background thread)
# ---------------------------------------------------------------------------

class MCPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ping':
            self._respond(200, {'status': 'ok', 'addin': 'Fusion360MCP', 'port': PORT,
                                'commands': list(COMMANDS)})
        else:
            self._respond(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/command':
            self._respond(404, {'error': 'use POST /command'})
            return
        if self.headers.get('Origin'):
            self._respond(403, {'error': 'browser requests are not allowed'})
            return
        if _TOKEN and not hmac.compare_digest(self.headers.get('X-MCP-Token', ''), _TOKEN):
            self._respond(403, {'error': 'missing or invalid X-MCP-Token'})
            return
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except Exception as e:
            self._respond(400, {'error': f'invalid JSON: {e}'})
            return

        with _request_lock:
            params = data.get('params', data.get('parameters', {}))
            _req_counter[0] += 1
            req_id = _req_counter[0]
            payload = json.dumps({'id': req_id, 'command': data.get('command', ''),
                                  'params': params})
            wait = min(max(int(params.get('timeout', 30)), 5), 300)
            _app.fireCustomEvent(EVENT_ID, payload)
            # Results of earlier requests that timed out may still arrive: skip them by id
            deadline = time.monotonic() + wait
            result = {'error': f'timeout: Fusion 360 did not respond within {wait} s'}
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    rid, res = _result_queue.get(timeout=left)
                except queue.Empty:
                    break
                if rid == req_id:
                    result = res
                    break

        self._respond(200, result)

    def _respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress HTTP access log


# ---------------------------------------------------------------------------
# Fusion custom event handler (main thread)
# ---------------------------------------------------------------------------

class FusionEventHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            event_args = adsk.core.CustomEventArgs.cast(args)
            data = json.loads(event_args.additionalInfo)
            req_id = data.get('id')
            result = execute_command(data['command'], data.get('params', {}))
        except Exception:
            req_id = locals().get('req_id')
            result = {'error': traceback.format_exc()}
        _result_queue.put((req_id, result))


# ---------------------------------------------------------------------------
# Add-in entry points
# ---------------------------------------------------------------------------

def run(context):
    global _app, _ui, _httpd, _http_thread, _custom_event, _event_handler, _TOKEN

    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        _TOKEN = _load_token()

        _custom_event = _app.registerCustomEvent(EVENT_ID)
        _event_handler = FusionEventHandler()
        _custom_event.add(_event_handler)

        _httpd = HTTPServer(('127.0.0.1', PORT), MCPRequestHandler)
        _http_thread = threading.Thread(target=_httpd.serve_forever, daemon=True)
        _http_thread.start()

        # No messageBox: it is modal and blocks the main thread when the add-in runs on startup
        _app.log(f'Fusion360 MCP started on port {PORT} (auth: {"on" if _TOKEN else "off, execute_python disabled"}).')
    except Exception:
        if _ui:
            _ui.messageBox(f'Fusion360MCP failed to start:\n{traceback.format_exc()}')


def stop(context):
    global _httpd, _custom_event

    try:
        if _httpd:
            _httpd.shutdown()
        if _custom_event and _app:
            _app.unregisterCustomEvent(EVENT_ID)
        adsk.terminate()
    except Exception:
        pass
