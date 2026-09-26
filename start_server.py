import http.server
import json
import zlib
import re
import math
import struct
import io
import os
import threading
import webbrowser
import time
import urllib.parse
from http import HTTPStatus
import ezdxf
from ezdxf import bbox

PORT = 8765
DIR = os.path.dirname(os.path.abspath(__file__))

# ─── PDF Content Stream Parser ────────────────────────────────────────────────

def find_page_height(data_bytes):
    """Extract page height from MediaBox."""
    text = data_bytes[:4096].decode('latin-1', errors='replace')
    m = re.search(r'/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]', text)
    if m:
        return float(m.group(4)) - float(m.group(2))
    return 841.89  # A4 default

def extract_flate_streams(data_bytes):
    """Find and decompress all FlateDecode streams from PDF binary."""
    streams = []
    i = 0
    b = data_bytes
    n = len(b)

    # Search for FlateDecode streams
    pat_filter = b'/Filter /FlateDecode'
    pat_filter2 = b'/Filter/FlateDecode'
    pat_stream = b'stream'

    positions = []
    seen_starts = set()
    for pat in [pat_filter, pat_filter2]:
        p = 0
        while True:
            pos = b.find(pat, p)
            if pos == -1:
                break
            positions.append(pos)
            p = pos + 1

    positions.sort()

    for fpos in positions:
        # Find the 'stream' keyword after this /Filter
        spos = b.find(pat_stream, fpos)
        if spos == -1 or spos - fpos > 512:
            continue
        # Skip 'stream' + newline
        dstart = spos + 6
        if dstart < n and b[dstart] == 13: dstart += 1  # \r
        if dstart < n and b[dstart] == 10: dstart += 1  # \n

        # Deduplicate: skip if we already processed this stream start
        if dstart in seen_starts:
            continue
        seen_starts.add(dstart)

        # Find 'endstream'
        epos = b.find(b'endstream', dstart)
        if epos == -1 or epos <= dstart:
            continue

        raw = b[dstart:epos]
        # Trim trailing whitespace
        while raw and raw[-1] in (10, 13, 32):
            raw = raw[:-1]

        try:
            dec = zlib.decompress(raw)
            text = dec.decode('latin-1', errors='replace')
            # Check it's a content stream (has path operators)
            if any(op in text for op in [' m\n', ' l\n', ' c\n', ' m\r', ' l\r', '\nm\n', '\nm ']):
                streams.append(text)
        except Exception:
            pass

    return streams

def tokenize(text):
    """Tokenize PDF content stream into numbers and operators."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Skip whitespace
        if c in ' \t\r\n':
            i += 1
            continue
        # Comment
        if c == '%':
            while i < n and text[i] != '\n':
                i += 1
            continue
        # Number
        if c in '0123456789.-' or (c == '+' and i+1 < n and text[i+1] in '0123456789.'):
            j = i
            if text[j] in '+-': j += 1
            while j < n and text[j] in '0123456789.':
                j += 1
            try:
                tokens.append(('n', float(text[i:j])))
            except:
                pass
            i = j
            continue
        # String (skip)
        if c == '(':
            depth = 1; i += 1
            while i < n and depth > 0:
                if text[i] == '\\': i += 2; continue
                if text[i] == '(': depth += 1
                elif text[i] == ')': depth -= 1
                i += 1
            continue
        # Hex string or dict (skip)
        if c == '<':
            if i+1 < n and text[i+1] == '<':
                depth = 1; i += 2
                while i < n and depth > 0:
                    if text[i] == '<' and i+1 < n and text[i+1] == '<': depth += 1; i += 2
                    elif text[i] == '>' and i+1 < n and text[i+1] == '>': depth -= 1; i += 2
                    else: i += 1
            else:
                while i < n and text[i] != '>': i += 1
                i += 1
            continue
        # Array
        if c in '[]':
            i += 1
            continue
        # Name
        if c == '/':
            j = i + 1
            while j < n and text[j] not in ' \t\r\n/[]<>()':
                j += 1
            tokens.append(('name', text[i:j]))
            i = j
            continue
        # Operator (letters, *, ', ")
        if c.isalpha() or c in '*\'"':
            j = i
            while j < n and (text[j].isalpha() or text[j] in '*\'"_'):
                j += 1
            tokens.append(('op', text[i:j]))
            i = j
            continue
        i += 1
    return tokens

def mul_ctm(a, b):
    """Multiply two 6-element CTM matrices [a b c d e f]."""
    return [
        a[0]*b[0] + a[2]*b[1],
        a[1]*b[0] + a[3]*b[1],
        a[0]*b[2] + a[2]*b[3],
        a[1]*b[2] + a[3]*b[3],
        a[0]*b[4] + a[2]*b[5] + a[4],
        a[1]*b[4] + a[3]*b[5] + a[5],
    ]

def apply_ctm(ctm, x, y):
    return (ctm[0]*x + ctm[2]*y + ctm[4],
            ctm[1]*x + ctm[3]*y + ctm[5])

def flatten_bezier(p0, p1, p2, p3, steps=12):
    """Flatten cubic bezier into polyline points."""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
        pts.append((x, y))
    return pts

def try_circle(pts_transformed, scale):
    """Try to fit a circle to a set of points."""
    if len(pts_transformed) < 3:
        return None
    xs = [p[0]*scale for p in pts_transformed]
    ys = [p[1]*scale for p in pts_transformed]
    mn_x, mx_x = min(xs), max(xs)
    mn_y, mx_y = min(ys), max(ys)
    cx = (mn_x + mx_x) / 2
    cy = (mn_y + mx_y) / 2
    rx = (mx_x - mn_x) / 2
    ry = (mx_y - mn_y) / 2
    if rx < 0.01 or ry < 0.01:
        return None
    if min(rx, ry) / max(rx, ry) < 0.80:
        return None  # too elliptical
    r = (rx + ry) / 2
    # Verify points lie on circle
    for x, y in zip(xs, ys):
        d = math.sqrt((x-cx)**2 + (y-cy)**2)
        if r > 0 and abs(d - r) / r > 0.30:
            return None
    return (cx, cy, r)

def parse_content_stream(text, scale, bezier_steps, do_circle, do_layer):
    """Parse PDF content stream and return list of DXF entities."""
    tokens = tokenize(text)
    entities = []
    layer_map = {}  # color_key -> layer_name

    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    ctm_stack = []
    path = []
    cur_x, cur_y = 0.0, 0.0
    stroke_color = (0, 0, 0)
    fill_color = (0, 0, 0)

    stack = []  # operand stack

    def get_layer(r, g, b):
        if not do_layer:
            return '0'
        key = f'{int(r*255)}_{int(g*255)}_{int(b*255)}'
        if key not in layer_map:
            layer_map[key] = f'L{len(layer_map)+1}'
        return layer_map[key]

    def tx(x, y):
        tx_x, tx_y = apply_ctm(ctm, x, y)
        return (tx_x * scale, tx_y * scale)

    def finalize_path(is_stroke):
        nonlocal path
        if not path:
            return
        color = stroke_color if is_stroke else fill_color
        layer = get_layer(*color)

        # Collect all endpoint coords for circle detection
        all_pts = []
        for seg in path:
            if seg[0] == 'M' or seg[0] == 'L':
                all_pts.append(tx(seg[1], seg[2]))
            elif seg[0] == 'C':
                all_pts.append(tx(seg[5], seg[6]))

        # Try circle detection
        if do_circle:
            curve_count = sum(1 for s in path if s[0] == 'C')
            move_count = sum(1 for s in path if s[0] == 'M')
            if 2 <= curve_count <= 8 and move_count == 1:
                circ = try_circle(all_pts, 1.0)  # already scaled in tx()
                if circ:
                    entities.append(('CIRCLE', layer, circ[0], circ[1], circ[2]))
                    path = []
                    return

        # Convert to polyline
        poly_pts = []
        first_pt = None
        for seg in path:
            if seg[0] == 'M':
                if poly_pts and len(poly_pts) > 1:
                    entities.append(('POLY', layer, poly_pts[:]))
                poly_pts = [tx(seg[1], seg[2])]
                first_pt = poly_pts[0]
            elif seg[0] == 'L':
                poly_pts.append(tx(seg[1], seg[2]))
            elif seg[0] == 'C':
                prev = poly_pts[-1] if poly_pts else (0, 0)
                p1 = tx(seg[1], seg[2])
                p2 = tx(seg[3], seg[4])
                p3 = tx(seg[5], seg[6])
                flat = flatten_bezier(prev, p1, p2, p3, bezier_steps)
                poly_pts.extend(flat[1:])
            elif seg[0] == 'H':
                if first_pt and poly_pts:
                    poly_pts.append(first_pt)

        if poly_pts and len(poly_pts) > 1:
            entities.append(('POLY', layer, poly_pts[:]))
        path = []

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok[0] == 'n':
            stack.append(tok[1])
            i += 1
            continue
        if tok[0] != 'op':
            i += 1
            continue

        op = tok[1]

        if op == 'q':
            ctm_stack.append(ctm[:])
        elif op == 'Q':
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == 'cm':
            if len(stack) >= 6:
                m = [stack[-6], stack[-5], stack[-4], stack[-3], stack[-2], stack[-1]]
                ctm = mul_ctm(ctm, m)
                stack = stack[:-6]
        elif op == 'm':  # moveto
            if len(stack) >= 2:
                x, y = stack[-2], stack[-1]
                path.append(('M', x, y))
                cur_x, cur_y = x, y
                stack = stack[:-2]
        elif op == 'l':  # lineto
            if len(stack) >= 2:
                x, y = stack[-2], stack[-1]
                path.append(('L', x, y))
                cur_x, cur_y = x, y
                stack = stack[:-2]
        elif op == 'c':  # curveto
            if len(stack) >= 6:
                x1,y1,x2,y2,x3,y3 = stack[-6],stack[-5],stack[-4],stack[-3],stack[-2],stack[-1]
                path.append(('C', x1, y1, x2, y2, x3, y3))
                cur_x, cur_y = x3, y3
                stack = stack[:-6]
        elif op == 'v':  # curveto (first ctrl = current point)
            if len(stack) >= 4:
                x2,y2,x3,y3 = stack[-4],stack[-3],stack[-2],stack[-1]
                path.append(('C', cur_x, cur_y, x2, y2, x3, y3))
                cur_x, cur_y = x3, y3
                stack = stack[:-4]
        elif op == 'y':  # curveto (second ctrl = endpoint)
            if len(stack) >= 4:
                x1,y1,x3,y3 = stack[-4],stack[-3],stack[-2],stack[-1]
                path.append(('C', x1, y1, x3, y3, x3, y3))
                cur_x, cur_y = x3, y3
                stack = stack[:-4]
        elif op == 'h':  # closepath
            path.append(('H',))
        elif op == 'S':  # stroke
            finalize_path(True)
        elif op in ('f', 'F', 'f*'):  # fill
            finalize_path(False)
        elif op in ('B', 'B*', 'b', 'b*'):  # fill+stroke
            finalize_path(True)
        elif op == 'n':  # end path no paint
            path = []
        elif op == 'RG':  # stroke RGB
            if len(stack) >= 3:
                stroke_color = (stack[-3], stack[-2], stack[-1])
                stack = stack[:-3]
        elif op == 'rg':  # fill RGB
            if len(stack) >= 3:
                fill_color = (stack[-3], stack[-2], stack[-1])
                stack = stack[:-3]
        elif op == 'G':  # stroke gray
            if stack:
                g = stack[-1]; stroke_color = (g, g, g); stack.pop()
        elif op == 'g':  # fill gray
            if stack:
                g = stack[-1]; fill_color = (g, g, g); stack.pop()
        elif op == 'K':  # stroke CMYK
            if len(stack) >= 4: stack = stack[:-4]
        elif op == 'k':  # fill CMYK
            if len(stack) >= 4: stack = stack[:-4]
        elif op == 'w':  # line width
            if stack: stack.pop()
        elif op in ('J', 'j', 'M', 'd', 'i', 'ri'):
            if stack: stack.pop()
        elif op in ('W', 'W*'):  # clip path
            path = []
        else:
            # Unknown operators - clear operands that look like they were consumed
            stack = []

        i += 1

    return entities, layer_map

def build_dxf(all_entities, layer_map, unit_code):
    """Build AutoCAD-compatible DXF using ezdxf."""
    unit_factors = {
        1: (25.4 / 72.0, 4),   # mm -> INSUNITS 4
        4: (2.54 / 72.0, 5),   # cm -> INSUNITS 5
        6: (0.0254 / 72.0, 6), # m  -> INSUNITS 6
        2: (1.0 / 72.0, 1),    # in -> INSUNITS 1
    }
    uf, insunits = unit_factors.get(int(unit_code), (25.4 / 72.0, 4))

    doc = ezdxf.new('R2000', setup=True)
    doc.header['$INSUNITS'] = insunits
    doc.header['$MEASUREMENT'] = 1 if insunits in (4, 5, 6) else 0

    palette = [1, 2, 3, 4, 5, 6, 7, 30, 40, 50, 70, 80, 100, 120, 140, 160, 180, 200, 220, 240]
    for idx, name in enumerate(layer_map.values()):
        if name not in doc.layers:
            doc.layers.add(name=name, color=palette[idx % len(palette)])

    msp = doc.modelspace()
    num_circles = 0
    num_polylines = 0

    for ent in all_entities:
        if ent[0] == 'CIRCLE':
            _, layer, cx, cy, r = ent
            msp.add_circle(center=(cx * uf, cy * uf), radius=r * uf, dxfattribs={'layer': layer})
            num_circles += 1
        elif ent[0] == 'POLY':
            _, layer, pts = ent
            scaled = [(px * uf, py * uf) for px, py in pts]
            clean = [scaled[0]]
            for p in scaled[1:]:
                if abs(p[0] - clean[-1][0]) > 1e-6 or abs(p[1] - clean[-1][1]) > 1e-6:
                    clean.append(p)
            if len(clean) >= 2:
                msp.add_lwpolyline(clean, dxfattribs={'layer': layer})
                num_polylines += 1

    # Configure active viewport so drawing is centered and fully visible when opened in CAD
    try:
        box = bbox.extents(msp)
        if box.has_data:
            width = box.extmax.x - box.extmin.x
            height = box.extmax.y - box.extmin.y
            cx = (box.extmin.x + box.extmax.x) / 2.0
            cy = (box.extmin.y + box.extmax.y) / 2.0
            view_height = max(height, width * 0.75, 10.0) * 1.2
            doc.set_modelspace_vport(height=view_height, center=(cx, cy))
            doc.header['$EXTMIN'] = box.extmin
            doc.header['$EXTMAX'] = box.extmax
            doc.header['$LIMMIN'] = (box.extmin.x, box.extmin.y)
            doc.header['$LIMMAX'] = (box.extmax.x, box.extmax.y)
    except Exception:
        pass

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue(), num_polylines, num_circles

def apply_grid_snap(entities, unit_code, grid_snap, scale=1.0):
    """Snap circle centers to exact grid pitch (removes PDF floating point noise e.g. 250.03 -> 250.00)."""
    if grid_snap <= 0.0:
        return entities

    unit_factors = {
        1: 25.4 / 72.0,   # mm
        4: 2.54 / 72.0,   # cm
        6: 0.0254 / 72.0, # m
        2: 1.0 / 72.0,    # in
    }
    uf = unit_factors.get(int(unit_code), 25.4 / 72.0)
    step = float(grid_snap) * scale

    circles = [e for e in entities if e[0] == 'CIRCLE']
    if len(circles) < 3:
        return entities

    import statistics
    xs = [c[2] * uf for c in circles]
    ys = [c[3] * uf for c in circles]

    med_x = statistics.median([x % step for x in xs])
    med_y = statistics.median([y % step for y in ys])
    tol = step * 0.25  # Only snap circles close to grid pitch

    new_entities = []
    for ent in entities:
        if ent[0] == 'CIRCLE':
            typ, layer, cx, cy, r = ent
            x_val = cx * uf
            y_val = cy * uf
            sx = med_x + round((x_val - med_x) / step) * step
            sy = med_y + round((y_val - med_y) / step) * step
            if abs(x_val - sx) <= tol:
                x_val = sx
            if abs(y_val - sy) <= tol:
                y_val = sy
            new_entities.append((typ, layer, x_val / uf, y_val / uf, r))
        else:
            new_entities.append(ent)

    return new_entities

def convert_pdf_to_dxf(pdf_bytes, unit_code=1, scale=1.0, bezier_steps=12,
                        do_circle=True, do_layer=True, grid_snap=0.0):
    """Main conversion function."""
    page_h = find_page_height(pdf_bytes)
    streams = extract_flate_streams(pdf_bytes)

    if not streams:
        return build_dxf([], {}, unit_code)

    # Join all streams into a unified content stream so transformation matrices (CTM)
    # and graphics state (q/Q) flow seamlessly across stream chunks without resetting.
    unified_stream = '\n'.join(streams)
    all_entities, all_layer_map = parse_content_stream(
        unified_stream, scale, bezier_steps, do_circle, do_layer
    )

    if grid_snap > 0.0:
        all_entities = apply_grid_snap(all_entities, unit_code, grid_snap, scale)

    return build_dxf(all_entities, all_layer_map, unit_code)

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

def get_html():
    path = os.path.join(DIR, 'index.html')
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read()
    return b'Not found'

class PDFDXFHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress request logs

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/' or path == '/index.html':
            content = get_html() or b'<h1>index.html not found</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            # Serve static files
            fpath = os.path.join(DIR, path.lstrip('/'))
            if os.path.exists(fpath) and os.path.isfile(fpath):
                ext = os.path.splitext(fpath)[1]
                ct = {'.js': 'application/javascript', '.css': 'text/css',
                      '.pdf': 'application/pdf'}.get(ext, 'application/octet-stream')
                with open(fpath, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', ct)
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)

    def do_POST(self):
        if self.path not in ('/convert', '/api/convert'):
            self.send_error(404)
            return

        try:
            # Parse multipart form data
            content_type = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)

            # Parse params from form
            boundary = None
            for part in content_type.split(';'):
                part = part.strip()
                if part.startswith('boundary='):
                    boundary = part[9:].strip()
                    break

            if not boundary:
                self.send_error(400, 'No boundary')
                return

            # Parse multipart
            params = {}
            pdf_data = None
            filename = 'output.pdf'

            parts = body.split(('--' + boundary).encode())
            for part in parts:
                if b'\r\n\r\n' not in part:
                    continue
                header_section, _, content = part.partition(b'\r\n\r\n')
                # Remove trailing \r\n--
                content = content.rstrip(b'\r\n')

                header_str = header_section.decode('utf-8', errors='replace')
                if 'name="pdf"' in header_str:
                    pdf_data = content
                    # Extract filename
                    m = re.search(r'filename="([^"]+)"', header_str)
                    if m:
                        filename = m.group(1)
                elif 'name="unit"' in header_str:
                    params['unit'] = content.decode().strip()
                elif 'name="scale"' in header_str:
                    params['scale'] = content.decode().strip()
                elif 'name="bezier"' in header_str:
                    params['bezier'] = content.decode().strip()
                elif 'name="circle"' in header_str:
                    params['circle'] = content.decode().strip()
                elif 'name="layer"' in header_str:
                    params['layer'] = content.decode().strip()
                elif 'name="grid"' in header_str:
                    params['grid'] = content.decode().strip()

            if not pdf_data:
                self.send_error(400, 'No PDF data')
                return

            unit_code = int(params.get('unit', '1'))
            scale = float(params.get('scale', '1.0'))
            bezier_steps = int(params.get('bezier', '12'))
            do_circle = params.get('circle', 'true') == 'true'
            do_layer = params.get('layer', 'true') == 'true'
            grid_snap = float(params.get('grid', '0.0'))

            dxf_content, n_poly, n_circ = convert_pdf_to_dxf(
                pdf_data, unit_code, scale, bezier_steps, do_circle, do_layer, grid_snap
            )

            dxf_bytes = dxf_content.encode('utf-8')
            dxf_filename = os.path.splitext(filename)[0] + '.dxf'

            resp_json = json.dumps({
                'success': True,
                'filename': dxf_filename,
                'size': len(dxf_bytes),
                'polylines': n_poly,
                'circles': n_circ,
                'dxf_b64': __import__('base64').b64encode(dxf_bytes).decode()
            })

            resp_bytes = resp_json.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(resp_bytes))
            self.end_headers()
            self.wfile.write(resp_bytes)

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            resp = json.dumps({'success': False, 'error': str(e), 'trace': err}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(resp))
            self.end_headers()
            self.wfile.write(resp)


def open_browser_delayed():
    time.sleep(1.0)
    webbrowser.open("http://localhost:" + str(PORT))

if __name__ == '__main__':
    threading.Thread(target=open_browser_delayed, daemon=True).start()
    print("PDF to DXF Converter Server")
    print("URL: http://localhost:" + str(PORT))
    print("Press Ctrl+C to stop")
    try:
        httpd = http.server.HTTPServer(("", PORT), PDFDXFHandler)
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
