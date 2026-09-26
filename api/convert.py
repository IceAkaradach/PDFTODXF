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
    """Find and decompress all FlateDecode streams from PDF binary safely and robustly."""
    streams = []
    seen_starts = set()
    b = data_bytes
    n = len(b)
    pat_stream = b'stream'
    p = 0
    while True:
        spos = b.find(pat_stream, p)
        if spos == -1:
            break
        p = spos + 6

        # Check dictionary preceding stream
        header = b[max(0, spos - 512):spos]
        if b'/Filter' in header and (b'/FlateDecode' in header or b'/Fl' in header):
            dstart = spos + 6
            if dstart < n and b[dstart] == 13: dstart += 1  # \r
            if dstart < n and b[dstart] == 10: dstart += 1  # \n

            if dstart in seen_starts:
                continue
            seen_starts.add(dstart)

            text = None
            # Strategy 1: zlib.decompressobj() automatically handles zlib stream boundaries cleanly
            try:
                d = zlib.decompressobj()
                dec = d.decompress(b[dstart:])
                text = dec.decode('latin-1', errors='replace')
            except Exception:
                pass

            # Strategy 2: If /Length is specified in header
            if not text:
                m_len = re.search(rb'/Length\s+(\d+)', header)
                if m_len:
                    slen = int(m_len.group(1))
                    try:
                        dec = zlib.decompress(b[dstart:dstart + slen])
                        text = dec.decode('latin-1', errors='replace')
                    except Exception:
                        pass

            # Strategy 3: Find endstream without stripping 0x20 space
            if not text:
                epos = b.find(b'endstream', dstart)
                if epos > dstart:
                    raw = b[dstart:epos]
                    while raw and raw[-1] in (10, 13):  # only strip newline, NEVER space 0x20
                        raw = raw[:-1]
                    try:
                        dec = zlib.decompress(raw)
                        text = dec.decode('latin-1', errors='replace')
                    except Exception:
                        pass

            if text and any(op in text for op in [' m\n', ' l\n', ' c\n', ' m\r', ' l\r', '\nm\n', '\nm ']):
                streams.append((spos, text))

    streams.sort(key=lambda s: s[0])
    return [s[1] for s in streams]

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
    layer_map = {}  # color_key -> dict(name=..., rgb=...)

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
        ir, ig, ib = int(round(r * 255)), int(round(g * 255)), int(round(b * 255))
        key = f'{ir}_{ig}_{ib}'
        if key not in layer_map:
            layer_map[key] = {'name': f'L{len(layer_map)+1}', 'rgb': (ir, ig, ib)}
        return layer_map[key]['name']

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

def rgb_to_best_aci(r, g, b):
    """Map RGB to closest AutoCAD standard Color Index (ACI 1-7)."""
    if r < 35 and g < 35 and b < 35:
        return 7  # Dark/Black -> AutoCAD White (ACI 7) for dark background visibility
    if r > 230 and g > 230 and b > 230:
        return 7
    std_colors = [
        (1, 255, 0, 0),     # Red
        (2, 255, 255, 0),   # Yellow
        (3, 0, 255, 0),     # Green
        (4, 0, 255, 255),   # Cyan
        (5, 0, 0, 255),     # Blue
        (6, 255, 0, 255),   # Magenta
    ]
    best_aci = 7
    best_d = float('inf')
    for aci, cr, cg, cb in std_colors:
        d = (r - cr)**2 + (g - cg)**2 + (b - cb)**2
        if d < best_d:
            best_d = d
            best_aci = aci
    return best_aci

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

    for info in layer_map.values():
        if isinstance(info, dict):
            name = info['name']
            r, g, b = info['rgb']
        else:
            name = str(info)
            r, g, b = (128, 128, 128)

        if name not in doc.layers:
            aci = rgb_to_best_aci(r, g, b)
            if r < 35 and g < 35 and b < 35:
                doc.layers.add(name=name, color=7)
            else:
                doc.layers.add(name=name, color=aci, true_color=ezdxf.colors.rgb2int((r, g, b)))

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

def align_and_calibrate_grid(entities, unit_code, grid_snap, scale=1.0):
    """Rigidly calibrates scale, aligns origin to (0,0), and regularizes hole grid pitch
    so dimensions in AutoCAD become exactly 250.00 and 125.00 without any contour distortion."""
    if grid_snap <= 0.0:
        return entities

    unit_factors = {
        1: 25.4 / 72.0,   # mm
        4: 2.54 / 72.0,   # cm
        6: 0.0254 / 72.0, # m
        2: 1.0 / 72.0,    # in
    }
    uf = unit_factors.get(int(unit_code), 25.4 / 72.0)
    target_pitch = float(grid_snap) * scale * 2.0  # e.g. 1.25 * 2 = 2.5 mm

    circles = [e for e in entities if e[0] == 'CIRCLE']
    if len(circles) < 3:
        return entities

    from collections import defaultdict
    import statistics

    # 1. Pitch detection: measure average horizontal distance between circles in rows
    rows = defaultdict(list)
    for c in circles:
        rows[round(c[3] * uf, 2)].append(c[2] * uf)

    best_row = max(rows.values(), key=len)
    best_row.sort()
    dxs = [best_row[i+1] - best_row[i] for i in range(len(best_row)-1)
           if 0.5 * target_pitch < best_row[i+1] - best_row[i] < 1.5 * target_pitch]
    
    if dxs:
        avg_pitch = sum(dxs) / len(dxs)
        calib_scale = target_pitch / avg_pitch
    else:
        calib_scale = 1.0

    # 2. Shift origin to (0, 0)
    all_xs = [e[2] * uf * calib_scale for e in entities if e[0] == 'CIRCLE']
    all_ys = [e[3] * uf * calib_scale for e in entities if e[0] == 'CIRCLE']
    for e in entities:
        if e[0] == 'POLY':
            for p in e[2]:
                all_xs.append(p[0] * uf * calib_scale)
                all_ys.append(p[1] * uf * calib_scale)

    origin_x = min(all_xs)
    origin_y = min(all_ys)

    # 3. Regularize circle grid (rows and columns)
    step_y = float(grid_snap) * scale
    step_x = step_y * 2.0  # e.g. 2.5 mm

    c_by_y = defaultdict(list)
    for e in entities:
        if e[0] == 'CIRCLE':
            y_grid = round((e[3] * uf * calib_scale - origin_y) / step_y) * step_y
            c_by_y[y_grid].append(e)

    final_entities = []
    for expected_y, clist in c_by_y.items():
        if len(clist) >= 3:
            clist.sort(key=lambda c: c[2])
            first_x = clist[0][2] * uf * calib_scale - origin_x
            base_x = round(first_x / step_y) * step_y
            for c in clist:
                typ, layer, cx, cy, r = c
                curr_x = cx * uf * calib_scale - origin_x
                idx = round((curr_x - base_x) / step_x)
                reg_x = base_x + idx * step_x
                reg_y = expected_y
                final_entities.append((typ, layer, reg_x / uf, reg_y / uf, r * calib_scale))
        else:
            for c in clist:
                typ, layer, cx, cy, r = c
                curr_x = cx * uf * calib_scale - origin_x
                curr_y = cy * uf * calib_scale - origin_y
                final_entities.append((typ, layer, curr_x / uf, curr_y / uf, r * calib_scale))

    # 4. Rigidly transform polylines without ANY distortion
    for e in entities:
        if e[0] == 'POLY':
            typ, layer, pts = e
            new_pts = []
            for px, py in pts:
                new_px = (px * uf * calib_scale - origin_x) / uf
                new_py = (py * uf * calib_scale - origin_y) / uf
                new_pts.append((new_px, new_py))
            final_entities.append((typ, layer, new_pts))

    return final_entities

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
        all_entities = align_and_calibrate_grid(all_entities, unit_code, grid_snap, scale)

    return build_dxf(all_entities, all_layer_map, unit_code)

from http.server import BaseHTTPRequestHandler
import base64

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, 'Expected multipart/form-data')
                return

            m = re.search(r'boundary=([^\s;]+)', content_type)
            if not m:
                self.send_error(400, 'Missing boundary')
                return
            boundary = m.group(1).strip('"')

            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)

            params = {}
            pdf_data = None
            filename = 'output.pdf'

            parts = body.split(('--' + boundary).encode())
            for part in parts:
                if b'\r\n\r\n' not in part:
                    continue
                header_section, _, content = part.partition(b'\r\n\r\n')
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                elif content.endswith(b'\n'):
                    content = content[:-1]
                header_str = header_section.decode('utf-8', errors='replace')

                if 'name="pdf"' in header_str:
                    pdf_data = content
                    fn_match = re.search(r'filename="([^"]+)"', header_str)
                    if fn_match:
                        filename = fn_match.group(1)
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
                'dxf_b64': base64.b64encode(dxf_bytes).decode()
            })

            resp_bytes = resp_json.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(resp_bytes))
            self.end_headers()
            self.wfile.write(resp_bytes)

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            resp = json.dumps({'success': False, 'error': str(e), 'trace': err}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(resp))
            self.end_headers()
            self.wfile.write(resp)
