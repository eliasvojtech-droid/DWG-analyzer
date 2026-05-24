import sys, os, json, math, re, tempfile, shutil, subprocess, threading, webbrowser, time
from concurrent.futures import ThreadPoolExecutor
import ezdxf
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

# cache: path -> (mtime, (la, ia, ta, ha, all_pts, layers))
_geom_cache: dict = {}
_geom_cache_lock = threading.Lock()

# ── Najdi ODA ─────────────────────────────────────────────────────────────────
def find_oda():
    import string
    for d in string.ascii_uppercase:
        for pf in ["Program Files", "Program Files (x86)"]:
            base = os.path.join(d + ":\\", pf, "ODA")
            if not os.path.exists(base): continue
            for sub in sorted(os.listdir(base), reverse=True):
                exe = os.path.join(base, sub, "ODAFileConverter.exe")
                if os.path.exists(exe): return exe
    return None

# ── Převod DWG → DXF ──────────────────────────────────────────────────────────
def convert_dwg_to_dxf(dwg_path):
    oda = find_oda()
    if not oda: raise Exception("ODA File Converter nenalezen")
    tmp_in = tempfile.mkdtemp()
    tmp_out = tempfile.mkdtemp()
    try:
        shutil.copy2(dwg_path, os.path.join(tmp_in, os.path.basename(dwg_path)))
        subprocess.run([oda, tmp_in, tmp_out, "ACAD2018", "DXF", "0", "1", "*.DWG"],
                      timeout=60, capture_output=True)
        dxf_files = [f for f in os.listdir(tmp_out) if f.lower().endswith(".dxf")]
        if not dxf_files: raise Exception("Převod selhal")
        out = tempfile.mktemp(suffix=".dxf")
        shutil.copy2(os.path.join(tmp_out, dxf_files[0]), out)
        return out
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)

# ── Parser DXF ────────────────────────────────────────────────────────────────
def get_block_real_name(entity, doc):
    """Získej skutečný název bloku - pro dynamické bloky vrátí jméno místo *U..."""
    name = entity.dxf.name
    if not name.startswith('*'):
        return name
    try:
        block = doc.blocks.get(name)
        if block:
            try:
                xdata = block.block_record.get_xdata('ACAD')
                if xdata:
                    for code, val in xdata:
                        if code == 1000 and val and not val.startswith('*'):
                            return val
            except: pass
    except: pass
    if name.startswith('*U'):
        return f'DynamicBlock_{name[2:]}'
    return name


def get_block_real_name(entity, doc):
    """Získej skutečný název bloku - pro dynamické bloky vrátí jméno místo *U..."""
    name = entity.dxf.name
    if not name.startswith('*'):
        return name
    # Dynamický blok - hledej skutečný název
    try:
        block = doc.blocks.get(name)
        if block:
            # Způsob 1: xdata ACAD obsahuje skutečný název
            try:
                xdata = block.block_record.get_xdata('ACAD')
                if xdata:
                    for code, val in xdata:
                        if code == 1000 and val and not val.startswith('*'):
                            return val
            except: pass
            # Způsob 2: extension dictionary
            try:
                if block.block_record.has_extension_dict:
                    edict = block.block_record.get_extension_dict()
                    if 'ACAD_ENHANCEDBLOCK' in edict:
                        return name  # má enhanced block ale název neznáme
            except: pass
            # Způsob 3: hledej blok se stejnou geometrií ale normálním názvem
            # (některé DXF exporty mají *U bloky jako kopie normálních bloků)
    except: pass
    # Fallback: odstraň *U prefix a použij jako název
    if name.startswith('*U'):
        return f'DynamicBlock_{name[2:]}'
    return name


def build_dynamic_block_map(doc):
    """Mapování anonymních *U bloků na jejich skutečné názvy"""
    mapping = {}
    for handle, entity in doc.entitydb.items():
        try:
            if entity.dxftype() != 'BLOCK_RECORD': continue
            name = entity.dxf.name
            if not name.startswith('*U'): continue
            # Způsob: xdata AcDbBlockRepBTag -> handle na parent block record
            try:
                xd = list(entity.get_xdata('AcDbBlockRepBTag'))
                for code, val in xd:
                    if code == 1005:
                        parent = doc.entitydb.get(val)
                        if parent and hasattr(parent.dxf,'name') and not parent.dxf.name.startswith('*'):
                            mapping[name] = parent.dxf.name; break
            except: pass
        except: pass
    return mapping

def parse_dxf(dxf_path):
    doc = ezdxf.readfile(dxf_path)
    dyn_map = build_dynamic_block_map(doc)
    block_map = {}
    def process(space):
        for entity in space:
            if entity.dxftype() == "INSERT":
                raw_name = entity.dxf.name
                if raw_name.startswith('*') and not raw_name.startswith('*U'):
                    continue
                name = dyn_map.get(raw_name, raw_name) if raw_name.startswith('*U') else raw_name
                x = float(entity.dxf.insert.x)
                y = float(entity.dxf.insert.y)
                rot = float(entity.dxf.get("rotation", 0))
                attrs = []
                if entity.attribs_follow:
                    for a in entity.attribs:
                        attrs.append({"tag": a.dxf.tag, "value": a.dxf.text})
                if name not in block_map:
                    block_map[name] = {"name": name, "count": 0, "inserts": []}
                block_map[name]["count"] += 1
                block_map[name]["inserts"].append({"x": x, "y": y, "rot": rot, "attrs": attrs})
    process(doc.modelspace())
    for layout in doc.layouts:
        if layout.name != "Model": process(layout)
    blocks = sorted(block_map.values(), key=lambda b: -b["count"])
    inserts = [{"name": b["name"], **i} for b in blocks for i in b["inserts"]]
    return {"blocks": blocks, "inserts": inserts, "totalInserts": len(inserts)}

# ── SVG náhled bloku ──────────────────────────────────────────────────────────
def get_block_geom(doc, bname, visited=None):
    if visited is None: visited = set()
    if bname in visited or bname not in doc.blocks: return [], [], []
    visited.add(bname)
    lines, arcs, circles = [], [], []
    for e in doc.blocks[bname]:
        try:
            t = e.dxftype()
            if t == 'LINE':
                s, en = e.dxf.start, e.dxf.end
                lines.append((s[0],s[1],en[0],en[1]))
            elif t == 'ARC':
                c, r = e.dxf.center, e.dxf.radius
                arcs.append((c[0],c[1],r,e.dxf.start_angle,e.dxf.end_angle))
            elif t == 'CIRCLE':
                c, r = e.dxf.center, e.dxf.radius
                circles.append((c[0],c[1],r))
            elif t == 'LWPOLYLINE':
                pts = list(e.get_points('xy'))
                for i in range(len(pts)-1):
                    lines.append((pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1]))
                if e.is_closed and len(pts)>1:
                    lines.append((pts[-1][0],pts[-1][1],pts[0][0],pts[0][1]))
            elif t == 'POLYLINE':
                pts = [(v.dxf.location[0],v.dxf.location[1]) for v in e.vertices]
                for i in range(len(pts)-1):
                    lines.append((pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1]))
            elif t == 'INSERT':
                sl, sa, sc = get_block_geom(doc, e.dxf.name, visited.copy())
                ix, iy = e.dxf.insert[0], e.dxf.insert[1]
                sx = getattr(e.dxf,'xscale',1) or 1
                sy = getattr(e.dxf,'yscale',1) or 1
                rot = math.radians(getattr(e.dxf,'rotation',0) or 0)
                def tr(px,py): return (px*sx*math.cos(rot)-py*sy*math.sin(rot)+ix, px*sx*math.sin(rot)+py*sy*math.cos(rot)+iy)
                for x1,y1,x2,y2 in sl:
                    a,b=tr(x1,y1); c2,d=tr(x2,y2); lines.append((a,b,c2,d))
                for cx2,cy2,r in sc:
                    a,b=tr(cx2,cy2); circles.append((a,b,r*sx))
                for cx2,cy2,r,sa2,ea in sa:
                    a,b=tr(cx2,cy2); arcs.append((a,b,r*sx,sa2+math.degrees(rot),ea+math.degrees(rot)))
        except: pass
    return lines, arcs, circles

def make_block_svg(lines, arcs, circles, w=120, h=80):
    from ezdxf.math import BoundingBox2d
    bb = BoundingBox2d()
    for x1,y1,x2,y2 in lines: bb.extend([(x1,y1),(x2,y2)])
    for cx,cy,r,*_ in arcs: bb.extend([(cx-r,cy-r),(cx+r,cy+r)])
    for cx,cy,r in circles: bb.extend([(cx-r,cy-r),(cx+r,cy+r)])
    if not bb.has_data: return None, None
    rw = bb.extmax[0]-bb.extmin[0] or 1
    rh = bb.extmax[1]-bb.extmin[1] or 1
    dims = {"w": round(rw,1), "h": round(rh,1)}
    px, py = rw*0.1, rh*0.1
    minX,minY = bb.extmin[0]-px, bb.extmin[1]-py
    totalW,totalH = rw+px*2, rh+py*2
    sc = min(w/totalW, h/totalH)
    def tx(x): return round((x-minX)*sc,2)
    def ty(y): return round(h-(y-minY)*sc,2)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 {w} {h}">']
    parts.append('<rect width="100%" height="100%" fill="#1e1e1e" rx="3"/>')
    for x1,y1,x2,y2 in lines:
        parts.append(f'<line x1="{tx(x1)}" y1="{ty(y1)}" x2="{tx(x2)}" y2="{ty(y2)}" stroke="#4a9eff" stroke-width="1"/>')
    for cx,cy,r,sa,ea in arcs:
        if ea<sa: ea+=360
        steps=max(8,int((ea-sa)/10))
        pts=[(cx+r*math.cos(math.radians(sa+(ea-sa)*i/steps)),cy+r*math.sin(math.radians(sa+(ea-sa)*i/steps))) for i in range(steps+1)]
        d=' '.join(f'{"M" if i==0 else "L"}{tx(p[0])},{ty(p[1])}' for i,p in enumerate(pts))
        parts.append(f'<path d="{d}" stroke="#4a9eff" stroke-width="1" fill="none"/>')
    for cx,cy,r in circles:
        parts.append(f'<circle cx="{tx(cx)}" cy="{ty(cy)}" r="{max(0.5,round(r*sc,2))}" stroke="#4ecb8d" stroke-width="1" fill="none"/>')
    parts.append('</svg>')
    return ''.join(parts), dims

# ── Extrakce geometrie pro diff ───────────────────────────────────────────────
def clean_mtext(txt):
    txt = re.sub(r'\\[pPAOCSfFlLqQhHWwBbcCiIoOtTkK][^;]*;', '', txt)
    txt = re.sub(r'\\[A-Za-z][0-9]*[;:]?', '', txt)
    txt = txt.replace('%%d','°').replace('%%D','°')
    txt = txt.replace('%%p','±').replace('%%P','±')
    txt = txt.replace('%%c','⌀').replace('%%C','⌀')
    txt = txt.replace('\\P',' ').replace('\\p',' ')
    return txt.strip()

def extract_geometry(msp):
    """Extrahuje geometrii včetně hladiny pro každou entitu"""
    lines, inserts, texts, hatches, all_pts = [], [], [], [], []
    for e in msp:
        try:
            t = e.dxftype()
            layer = e.dxf.layer if hasattr(e.dxf,'layer') else '0'
            if t == 'LINE':
                s,en = e.dxf.start, e.dxf.end
                length = round(math.hypot(en[0]-s[0],en[1]-s[1]),2)
                lines.append((round(s[0],1),round(s[1],1),round(en[0],1),round(en[1],1),layer,length))
                all_pts += [(s[0],s[1]),(en[0],en[1])]
            elif t == 'INSERT':
                x,y = e.dxf.insert[0],e.dxf.insert[1]
                rot = round(getattr(e.dxf,'rotation',0) or 0, 2)
                inserts.append((round(x,1),round(y,1),e.dxf.name,layer,rot))
                all_pts.append((x,y))
            elif t == 'LWPOLYLINE':
                pts = list(e.get_points('xy'))
                for p in pts: all_pts.append((p[0],p[1]))
                for i in range(len(pts)-1):
                    length = round(math.hypot(pts[i+1][0]-pts[i][0],pts[i+1][1]-pts[i][1]),2)
                    lines.append((round(pts[i][0],1),round(pts[i][1],1),round(pts[i+1][0],1),round(pts[i+1][1],1),layer,length))
                if e.is_closed and len(pts)>1:
                    length = round(math.hypot(pts[0][0]-pts[-1][0],pts[0][1]-pts[-1][1]),2)
                    lines.append((round(pts[-1][0],1),round(pts[-1][1],1),round(pts[0][0],1),round(pts[0][1],1),layer,length))
            elif t == 'POLYLINE':
                raw = [(v.dxf.location[0],v.dxf.location[1]) for v in e.vertices]
                filt = [(x,y) for x,y in raw if not (abs(x)<0.001 and abs(y)<0.001)]
                if len(filt)<2: continue
                sl = [math.hypot(filt[i+1][0]-filt[i][0],filt[i+1][1]-filt[i][1]) for i in range(len(filt)-1)]
                avg = sum(sl)/len(sl) if sl else 1
                thresh = max(avg*5,500)
                for p in filt: all_pts.append(p)
                for i in range(len(filt)-1):
                    if sl[i]<=thresh:
                        length = round(sl[i],2)
                        lines.append((round(filt[i][0],1),round(filt[i][1],1),round(filt[i+1][0],1),round(filt[i+1][1],1),layer,length))
            elif t in ('TEXT','MTEXT'):
                x,y = e.dxf.insert[0],e.dxf.insert[1]
                if t == 'MTEXT':
                    try: raw = e.plain_mtext()
                    except: raw = e.text if hasattr(e,'text') else ''
                else:
                    raw = e.dxf.text
                txt = clean_mtext(raw)[:80]
                if txt:
                    texts.append((round(x,1),round(y,1),txt,layer))
                    all_pts.append((x,y))
            elif t == 'DIMENSION':
                try:
                    x = e.dxf.text_midpoint[0] if hasattr(e.dxf,'text_midpoint') else e.dxf.defpoint[0]
                    y = e.dxf.text_midpoint[1] if hasattr(e.dxf,'text_midpoint') else e.dxf.defpoint[1]
                    actual = e.dxf.actual_measurement
                    txt = e.dxf.text if (hasattr(e.dxf,'text') and e.dxf.text and e.dxf.text != '<>') else f"{actual:.0f}"
                    txt = clean_mtext(txt)
                    if txt:
                        texts.append((round(x,1),round(y,1),txt,layer))
                        all_pts.append((x,y))
                except: pass
            elif t == 'HATCH':
                for path in e.paths:
                    pts2 = []
                    if hasattr(path,'edges'):
                        for edge in path.edges:
                            etype = type(edge).__name__
                            if etype == 'LineEdge':
                                pts2.append((round(edge.start[0],1),round(edge.start[1],1)))
                            elif etype == 'ArcEdge':
                                pts2.append((round(edge.center[0],1),round(edge.center[1],1)))
                    elif hasattr(path,'vertices'):
                        pts2 = [(round(v[0],1),round(v[1],1)) for v in path.vertices]
                    if len(pts2)>=2:
                        key = (layer, e.dxf.pattern_name, tuple(pts2[:6]))
                        hatches.append(key)
                        for p in pts2: all_pts.append(p)
        except: pass
    return lines, inserts, texts, hatches, all_pts


def make_diff_svg(la,ia,ta,ha,lb,ib,tb,hb,all_pts,layer_filter=None):
    # lines: (x1,y1,x2,y2,layer,length), inserts: (x,y,name,layer,rot), texts: (x,y,txt,layer)

    def line_geom(l): return l[:4]  # jen souřadnice pro porovnání
    def ins_geom(i): return i[:3]   # x,y,name

    # Filtr dle hladiny
    def flayer(items): 
        if not layer_filter: return items
        return [i for i in items if (i[4] if len(i)>4 else i[3]) == layer_filter]

    la2,lb2 = flayer(la),flayer(lb)
    ia2,ib2 = flayer(ia),flayer(ib)
    ta2,tb2 = ([i for i in ta if i[3]==layer_filter] if layer_filter else ta,
               [i for i in tb if i[3]==layer_filter] if layer_filter else tb)

    sla = set(line_geom(l) for l in la2)
    slb = set(line_geom(l) for l in lb2)
    sia = set(ins_geom(i) for i in ia2)
    sib = set(ins_geom(i) for i in ib2)
    sta = set((x,y,t,l) for x,y,t,l in ta2)
    stb = set((x,y,t,l) for x,y,t,l in tb2)
    sha = set(ha) if not layer_filter else set(h for h in ha if h[0]==layer_filter)
    shb = set(hb) if not layer_filter else set(h for h in hb if h[0]==layer_filter)

    # Délky per layer
    def layer_lengths(lines):
        d = {}
        for l in lines:
            layer = l[4] if len(l)>4 else '0'
            d[layer] = d.get(layer,0) + (l[5] if len(l)>5 else 0)
        return d
    ll_a = layer_lengths(la)
    ll_b = layer_lengths(lb)
    all_layers = sorted(set(list(ll_a.keys())+list(ll_b.keys())))
    layer_stats = []
    for layer in all_layers:
        da = round(ll_a.get(layer,0),1)
        db = round(ll_b.get(layer,0),1)
        diff_len = round(db-da,1)
        layer_stats.append({"layer":layer,"len_a":da,"len_b":db,"diff":diff_len})

    # Detekce přesunutých entit
    def line_key_angle(x1,y1,x2,y2):
        dx,dy=x2-x1,y2-y1; length=math.hypot(dx,dy)
        if length<0.01: return None,0
        nx,ny=dx/length,dy/length
        if nx<0 or (abs(nx)<0.001 and ny<0): nx,ny=-nx,-ny
        return (round(nx,3),round(ny,3)),round(length,1)

    # Přesunuté čáry - stejná délka+úhel, jiná pozice — O(n) přes dict
    moved_lines = []
    only_a_geom = sla - slb
    only_b_geom = slb - sla
    # Seskup B čáry dle (úhel, zaokrouhlená délka) pro O(1) lookup
    b_by_shape: dict = {}
    for lb_l in only_b_geom:
        ang, ln = line_key_angle(*lb_l)
        if ang:
            b_by_shape.setdefault((ang, round(ln, 1)), []).append(lb_l)
    used_moved_b = set()
    for la_l in list(only_a_geom):
        ang_a, len_a_m = line_key_angle(*la_l)
        if not ang_a: continue
        candidates = b_by_shape.get((ang_a, round(len_a_m, 1)), [])
        for lb_l in candidates:
            if lb_l in used_moved_b: continue
            if abs(len_a_m - line_key_angle(*lb_l)[1]) <= len_a_m * 0.01:
                moved_lines.append((la_l, lb_l))
                used_moved_b.add(lb_l)
                break
    moved_lines_a = set(m[0] for m in moved_lines)
    moved_lines_b = set(m[1] for m in moved_lines)

    # Přesunuté bloky - stejné jméno+rotace, jiná pozice — O(n) přes dict
    moved_inserts = []
    only_ia = sia - sib
    only_ib = sib - sia
    b_by_name: dict = {}
    for bi in only_ib:
        b_by_name.setdefault(bi[2], []).append(bi)
    used_moved_ib = set()
    for ai in list(only_ia):
        for bi in b_by_name.get(ai[2], []):
            if bi not in used_moved_ib:
                moved_inserts.append((ai, bi))
                used_moved_ib.add(bi)
                break
    moved_ins_a = set(m[0] for m in moved_inserts)
    moved_ins_b = set(m[1] for m in moved_inserts)

    # Detekce prodloužení/zkrácení
    def line_key_collinear(x1,y1,x2,y2):
        dx,dy=x2-x1,y2-y1; length=math.hypot(dx,dy)
        if length<0.01: return None
        nx,ny=dx/length,dy/length
        if nx<0 or (abs(nx)<0.001 and ny<0): nx,ny=-nx,-ny
        return (round(nx,3),round(ny,3),round(abs(x1*ny-y1*nx),1))

    # Seskup B čáry dle kolineárního klíče pro O(n) lookup
    b_only_r = (slb - sla) - moved_lines_b
    b_collinear: dict = {}
    for lb_l in b_only_r:
        k = line_key_collinear(*lb_l)
        if k:
            b_collinear.setdefault(k, []).append(lb_l)
    resized=[]; used_r=set()
    for ll in list((sla-slb)-moved_lines_a):
        x1a,y1a,x2a,y2a=ll; key_a=line_key_collinear(*ll)
        if not key_a: continue
        len_a2=math.hypot(x2a-x1a,y2a-y1a); best=None; best_s=float('inf')
        for lb_l in b_collinear.get(key_a, []):
            if lb_l in used_r: continue
            x1b,y1b,x2b,y2b=lb_l
            share=(abs(x1a-x1b)<2 and abs(y1a-y1b)<2) or (abs(x1a-x2b)<2 and abs(y1a-y2b)<2) or \
                  (abs(x2a-x1b)<2 and abs(y2a-y1b)<2) or (abs(x2a-x2b)<2 and abs(y2a-y2b)<2)
            if share:
                len_b2=math.hypot(x2b-x1b,y2b-y1b); s=abs(len_b2-len_a2)
                if s<best_s: best_s=s; best=lb_l
        if best:
            len_b2=math.hypot(best[2]-best[0],best[3]-best[1]); delta=round(len_b2-len_a2,1)
            if abs(delta)>1: resized.append((ll,best,delta,round(len_a2,1),round(len_b2,1)))
            used_r.add(best)
    resized_a=set(r[0] for r in resized); resized_b=set(r[1] for r in resized)

    if not all_pts: return None
    xs=sorted(p[0] for p in all_pts); ys=sorted(p[1] for p in all_pts); n=len(xs)
    minX=xs[max(0,int(n*0.001))]; maxX=xs[min(n-1,int(n*0.999))]
    minY=ys[max(0,int(n*0.001))]; maxY=ys[min(n-1,int(n*0.999))]
    rw=maxX-minX or 1; rh=maxY-minY or 1
    minX-=rw*0.1; maxX+=rw*0.1; minY-=rh*0.1; maxY+=rh*0.1
    totalW=maxX-minX; totalH=maxY-minY; VW,VH=1000,580

    def tx(x): return round((x-minX)/totalW*VW,2)
    def ty(y): return round((1-(y-minY)/totalH)*VH,2)
    def inv(v,vm): return -50<=v<=vm+50

    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 {VW} {VH}" preserveAspectRatio="xMidYMid meet">']
    parts.append(f'<rect width="{VW}" height="{VH}" fill="#1a1f26"/>')

    def dl(x1,y1,x2,y2,col,w=0.8,extra=''):
        px1,py1,px2,py2=tx(x1),ty(y1),tx(x2),ty(y2)
        if inv(px1,VW) and inv(px2,VW) and inv(py1,VH) and inv(py2,VH) and (abs(px1-px2)>0.1 or abs(py1-py2)>0.1):
            parts.append(f'<line x1="{px1}" y1="{py1}" x2="{px2}" y2="{py2}" stroke="{col}" stroke-width="{w}" opacity="0.9" {extra}/>')

    def dc(x,y,col,sz=4,extra=''):
        cx,cy=tx(x),ty(y)
        if inv(cx,VW) and inv(cy,VH):
            parts.append(f'<line x1="{cx-sz}" y1="{cy}" x2="{cx+sz}" y2="{cy}" stroke="{col}" stroke-width="2" {extra}/>')
            parts.append(f'<line x1="{cx}" y1="{cy-sz}" x2="{cx}" y2="{cy+sz}" stroke="{col}" stroke-width="2" {extra}/>')

    def dt(x,y,col,txt=''):
        cx,cy=tx(x),ty(y)
        if not (inv(cx,VW) and inv(cy,VH)): return
        safe=txt.replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
        short=(txt[:30]+'\u2026') if len(txt)>30 else txt
        ss=short.replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
        parts.append(f'<text x="{cx}" y="{cy}" fill="{col}" font-family="monospace" font-size="10" opacity="0.95" class="diff-text" data-tip="{safe}" style="cursor:default">{ss}</text>')

    # Čáry
    for l in sla&slb: dl(*l[:4],'#4a5568')
    for l in (sla-slb)-resized_a-moved_lines_a: dl(*l[:4],'#e06c75',1.5)
    for l in (slb-sla)-resized_b-moved_lines_b: dl(*l[:4],'#4ecb8d',1.5)
    for l in resized_a: dl(*l[:4],'#f0a855',1.5)
    for l in resized_b: dl(*l[:4],'#f0a855',1.5)
    # Přesunuté čáry - fialová
    for la_l,lb_l in moved_lines:
        dl(*la_l[:4],'#c678dd',1,'stroke-dasharray="4,3"')
        dl(*lb_l[:4],'#c678dd',1.5)
        # Šipka od středu staré k středu nové
        mx1=tx((la_l[0]+la_l[2])/2); my1=ty((la_l[1]+la_l[3])/2)
        mx2=tx((lb_l[0]+lb_l[2])/2); my2=ty((lb_l[1]+lb_l[3])/2)
        if inv(mx1,VW) and inv(mx2,VW) and inv(my1,VH) and inv(my2,VH):
            parts.append(f'<line x1="{mx1}" y1="{my1}" x2="{mx2}" y2="{my2}" stroke="#c678dd" stroke-width="1" stroke-dasharray="2,2" opacity="0.6"/>')

    # Bloky
    for x,y,nm in sia&sib: dc(x,y,'#4a9eff')
    for x,y,nm in (sia-sib)-moved_ins_a: dc(x,y,'#e06c75',7)
    for x,y,nm in (sib-sia)-moved_ins_b: dc(x,y,'#4ecb8d',7)
    for ai,bi in moved_inserts:
        dc(ai[0],ai[1],'#c678dd',7,'stroke-dasharray="3,2"')
        dc(bi[0],bi[1],'#c678dd',7)
        mx1,my1=tx(ai[0]),ty(ai[1]); mx2,my2=tx(bi[0]),ty(bi[1])
        if inv(mx1,VW) and inv(mx2,VW) and inv(my1,VH) and inv(my2,VH):
            parts.append(f'<line x1="{mx1}" y1="{my1}" x2="{mx2}" y2="{my2}" stroke="#c678dd" stroke-width="1" stroke-dasharray="2,2" opacity="0.6" marker-end="url(#arrow)"/>')

    # Texty
    for x,y,t2,l in sta&stb: dt(x,y,'#6a7a8a',t2)
    for x,y,t2,l in sta-stb: dt(x,y,'#e06c75',t2)
    for x,y,t2,l in stb-sta: dt(x,y,'#4ecb8d',t2)

    # Popisky délkových změn
    for old_l,new_l,delta,len_a3,len_b3 in resized:
        x1,y1,x2,y2=new_l[:4]
        px1,py1,px2,py2=tx(x1),ty(y1),tx(x2),ty(y2)
        if not (inv(px1,VW) and inv(px2,VW) and inv(py1,VH) and inv(py2,VH)): continue
        sign='+' if delta>0 else ''
        action='prodloužena' if delta>0 else 'zkrácena'
        tip=f'{action} {sign}{delta:.1f} ({len_a3:.1f}→{len_b3:.1f})'
        parts.append(f'<line x1="{px1}" y1="{py1}" x2="{px2}" y2="{py2}" stroke="transparent" stroke-width="12" class="diff-resized" data-tip="{tip}"/>')

    # Definice šipky
    parts.insert(1,'<defs><marker id="arrow" markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#c678dd" opacity="0.7"/></marker></defs>')

    # Legenda
    rmv=len((sla-slb)-resized_a-moved_lines_a)+len((sia-sib)-moved_ins_a)+len(sta-stb)+len(sha-shb)
    add=len((slb-sla)-resized_b-moved_lines_b)+len((sib-sia)-moved_ins_b)+len(stb-sta)+len(shb-sha)
    mv=len(moved_lines)+len(moved_inserts)
    parts.append('<rect x="10" y="10" width="210" height="126" fill="rgba(0,0,0,0.82)" rx="4"/>')
    parts.append('<line x1="20" y1="28" x2="42" y2="28" stroke="#4a5568" stroke-width="2"/><text x="50" y="32" fill="#888" font-family="monospace" font-size="11">Beze změny</text>')
    parts.append(f'<line x1="20" y1="46" x2="42" y2="46" stroke="#e06c75" stroke-width="2"/><text x="50" y="50" fill="#e06c75" font-family="monospace" font-size="11">Smazáno ({rmv})</text>')
    parts.append(f'<line x1="20" y1="64" x2="42" y2="64" stroke="#4ecb8d" stroke-width="2"/><text x="50" y="68" fill="#4ecb8d" font-family="monospace" font-size="11">Přidáno ({add})</text>')
    parts.append(f'<line x1="20" y1="82" x2="42" y2="82" stroke="#f0a855" stroke-width="2"/><text x="50" y="86" fill="#f0a855" font-family="monospace" font-size="11">Změněná délka ({len(resized)})</text>')
    parts.append(f'<line x1="20" y1="100" x2="42" y2="100" stroke="#c678dd" stroke-width="2" stroke-dasharray="4,2"/><text x="50" y="104" fill="#c678dd" font-family="monospace" font-size="11">Přesunuto ({mv})</text>')
    parts.append('<text x="20" y="122" fill="#555" font-family="monospace" font-size="10">čáry · bloky · texty · šrafy</text>')
    parts.append('</svg>')

    return {
        "svg": ''.join(parts),
        "stats": {
            "removed":rmv,"added":add,"resized":len(resized),"moved":mv,
            "removed_lines":len((sla-slb)-resized_a-moved_lines_a),
            "added_lines":len((slb-sla)-resized_b-moved_lines_b),
            "removed_blocks":len((sia-sib)-moved_ins_a),
            "added_blocks":len((sib-sia)-moved_ins_b),
            "removed_texts":len(sta-stb),"added_texts":len(stb-sta),
            "removed_hatches":len(sha-shb),"added_hatches":len(shb-sha),
            "moved_lines":len(moved_lines),"moved_blocks":len(moved_inserts),
            "resized_details":[{"delta":r[2],"len_a":r[3],"len_b":r[4]} for r in resized[:20]],
            "layer_stats": layer_stats,
            "layers": all_layers,
        }
    }


def _load_geometry(path):
    """Konvertuje DWG→DXF, parsuje geometrii a cachuje výsledek dle mtime."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _geom_cache_lock:
        cached = _geom_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    dxf_path = convert_dwg_to_dxf(path)
    doc = ezdxf.readfile(dxf_path)
    os.unlink(dxf_path)
    geom = extract_geometry(doc.modelspace())  # la,ia,ta,ha,all_pts
    layers = sorted({e.dxf.layer for e in doc.modelspace() if hasattr(e.dxf, 'layer')})
    result = (*geom, layers)
    with _geom_cache_lock:
        _geom_cache[path] = (mtime, result)
    return result


@app.route('/api/compare_drawings', methods=['POST'])
def api_compare_drawings():
    data = request.json
    path_a, path_b = data['path_a'], data['path_b']
    layer_filter = data.get('layer_filter') or None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_a = ex.submit(_load_geometry, path_a)
            fut_b = ex.submit(_load_geometry, path_b)
            la,ia,ta,ha,pts_a,layers_a = fut_a.result()
            lb,ib,tb,hb,pts_b,layers_b = fut_b.result()
        result = make_diff_svg(la,ia,ta,ha,lb,ib,tb,hb,pts_a+pts_b,layer_filter)
        if not result: return jsonify({"ok":False,"error":"Prázdné výkresy"})
        layers = sorted(set(layers_a) | set(layers_b))
        return jsonify({"ok":True, **result, "layers": layers})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route('/api/open_file_dialog', methods=['POST'])
def api_open_file_dialog():
    """Otevře file dialog přes PowerShell"""
    multi = request.json.get('multi', True)
    try:
        if multi:
            ps = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.OpenFileDialog; $f.Filter="DWG files (*.dwg)|*.dwg"; $f.Multiselect=$true; if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){$f.FileNames -join "|"}else{""}'
        else:
            ps = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.OpenFileDialog; $f.Filter="DWG files (*.dwg)|*.dwg"; if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){$f.FileName}else{""}'
        result = subprocess.run(
            ['powershell','-noprofile','-windowstyle','hidden','-command',ps],
            capture_output=True, timeout=60,
            encoding='utf-8', errors='replace',
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        paths = [p.strip() for p in result.stdout.strip().split('|') if p.strip()]
        return jsonify({"paths": paths})
    except Exception as e:
        return jsonify({"paths": [], "error": str(e)})


# ── Flask routes ─────────────────────────────────────────────────────────────

BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/api/check_oda')
def api_check_oda():
    oda = find_oda()
    return jsonify({"found": oda is not None, "path": oda or ""})

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "Žádný soubor"})
    f = request.files['file']
    fname = f.filename
    tmp = tempfile.mktemp(suffix='.dwg')
    f.save(tmp)
    try:
        dxf_path = convert_dwg_to_dxf(tmp)
        result = parse_dxf(dxf_path)
        os.unlink(dxf_path)
        return jsonify({"ok": True, "file": fname, "path": fname, "tmp_path": tmp, **result})
    except Exception as e:
        try: os.unlink(tmp)
        except: pass
        return jsonify({"ok": False, "file": fname, "path": fname, "error": str(e)})

@app.route('/api/upload_and_store', methods=['POST'])
def api_upload_and_store():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "Žádný soubor"})
    f = request.files['file']
    fname = f.filename
    tmp = tempfile.mktemp(suffix='.dwg')
    f.save(tmp)
    return jsonify({"ok": True, "file": fname, "tmp_path": tmp})

@app.route('/api/block_previews', methods=['POST'])
def api_block_previews():
    data = request.json
    path = data.get('tmp_path') or data.get('path')
    try:
        dxf_path = convert_dwg_to_dxf(path)
        doc = ezdxf.readfile(dxf_path)
        os.unlink(dxf_path)
        previews = {}
        for block in doc.blocks:
            bname = block.name
            if bname.startswith('*'): continue
            try:
                lines, arcs, circles = get_block_geom(doc, bname)
                svg, dims = make_block_svg(lines, arcs, circles)
                previews[bname] = {"svg": svg, "dims": dims}
            except:
                previews[bname] = {"svg": None, "dims": None}
        return jsonify({"ok": True, "previews": previews})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/api/compare_blocks', methods=['POST'])
def api_compare_blocks():
    data = request.json
    path_a, path_b = data['path_a'], data['path_b']
    tol = float(data.get('tolerance', 1.0))
    try:
        dxf_a = convert_dwg_to_dxf(path_a)
        data_a = parse_dxf(dxf_a); os.unlink(dxf_a)
        dxf_b = convert_dwg_to_dxf(path_b)
        data_b = parse_dxf(dxf_b); os.unlink(dxf_b)
        map_a = {b["name"]: b for b in data_a["blocks"]}
        map_b = {b["name"]: b for b in data_b["blocks"]}
        all_names = sorted(set(list(map_a.keys()) + list(map_b.keys())))
        results = []
        for name in all_names:
            ins_a = map_a[name]["inserts"] if name in map_a else []
            ins_b = map_b[name]["inserts"] if name in map_b else []
            cnt_a, cnt_b = len(ins_a), len(ins_b)
            matched_a = [False]*cnt_a; matched_b = [False]*cnt_b; moved = []
            for i, ia in enumerate(ins_a):
                best_dist=tol; best_j=-1
                for j, ib in enumerate(ins_b):
                    if matched_b[j]: continue
                    dist=((ia["x"]-ib["x"])**2+(ia["y"]-ib["y"])**2)**0.5
                    if dist<best_dist: best_dist=dist; best_j=j
                if best_j>=0:
                    matched_a[i]=True; matched_b[best_j]=True; ib=ins_b[best_j]
                    if best_dist>0.001 or abs(ia["rot"]-ib["rot"])>0.1:
                        moved.append({"from":{"x":ia["x"],"y":ia["y"]},"to":{"x":ib["x"],"y":ib["y"]},"dist":round(best_dist,3)})
            removed=[ins_a[i] for i in range(cnt_a) if not matched_a[i]]
            added=[ins_b[j] for j in range(cnt_b) if not matched_b[j]]
            if cnt_a==cnt_b and not added and not removed and not moved: status="same"
            elif cnt_a==0: status="new"
            elif cnt_b==0: status="deleted"
            else: status="changed"
            results.append({"name":name,"count_a":cnt_a,"count_b":cnt_b,"diff":cnt_b-cnt_a,"added":added,"removed":removed,"moved":moved,"status":status})
        summary={"new":sum(1 for r in results if r["status"]=="new"),"deleted":sum(1 for r in results if r["status"]=="deleted"),"changed":sum(1 for r in results if r["status"]=="changed"),"same":sum(1 for r in results if r["status"]=="same"),"added_total":sum(len(r["added"]) for r in results),"removed_total":sum(len(r["removed"]) for r in results),"moved_total":sum(len(r["moved"]) for r in results)}
        return jsonify({"ok":True,"blocks":results,"summary":summary,"file_a":os.path.basename(path_a),"file_b":os.path.basename(path_b)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ── Chat / Claude API ─────────────────────────────────────────────────────────
@app.route('/api/has_api_key')
def api_has_api_key():
    return jsonify({"has_key": bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())})

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    message = (data.get('message') or '').strip()
    history = data.get('history') or []
    context = data.get('context') or ''
    api_key = (data.get('api_key') or '').strip() or os.environ.get('ANTHROPIC_API_KEY', '').strip()

    if not api_key:
        return jsonify({"ok": False, "error": "Chybí Anthropic API klíč. Zadejte ho v nastavení chatu."})
    if not message:
        return jsonify({"ok": False, "error": "Prázdná zpráva"})

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)

        system_prompt = (
            "Jsi AI asistent integrovaný do DWG Block Analyzer — desktopové aplikace "
            "pro analýzu a porovnávání technických výkresů DWG/DXF.\n\n"
            "Pomáháš s:\n"
            "- Analýzou výkresů (bloky, počty vložení, atributy)\n"
            "- Porovnáváním revizí výkresů (přidané/smazané/přesunuté entity)\n"
            "- Interpretací vizuálního diffu výkresů\n"
            "- Dotazy k formátu DWG/DXF a CAD workflow\n\n"
            "Odpovídej stručně a technicky přesně v češtině."
        )
        if context:
            system_prompt += f"\n\nStav aplikace:\n{context}"

        messages = [{"role": h["role"], "content": h["content"]} for h in history[-20:]]
        messages.append({"role": "user", "content": message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=messages
        )
        return jsonify({"ok": True, "response": response.content[0].text})
    except ImportError:
        return jsonify({"ok": False, "error": "Balíček 'anthropic' není nainstalován. Spusťte: pip install anthropic"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = 5000
    # Otevři prohlížeč po startu serveru
    def open_browser():
        time.sleep(1.2)
        webbrowser.open(f'http://localhost:{port}')
    threading.Thread(target=open_browser, daemon=True).start()
    print(f"DWG Block Analyzer běží na http://localhost:{port}")
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)
