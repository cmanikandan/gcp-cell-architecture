#!/usr/bin/env python3
"""
Renders Google Cloud Architecture Center (docs.cloud.google.com) style diagrams
using official Google Cloud technical SVG icons, official Roboto / Roboto Mono
fonts, and exact GCP2 layout conventions (flat #F6F6F6 Google Cloud container,
#E1F5FE / #F3E5F5 / #EFEBE9 pastel zones, crisp rx=2px white product cards with
subtle Material elevation, doubleRect multi-instance cards, orthogonal paths,
numbered step circles, and clean bottom step legends).
"""

import base64
import html
import subprocess
from pathlib import Path
from PIL import Image

HERE = Path(__file__).resolve().parent
ICONS_DIR = HERE / "icons"
FONT_ROBOTO = Path("/tmp/google-fonts/ofl/roboto/Roboto[wdth,wght].ttf")
FONT_MONO = Path("/tmp/google-fonts/ofl/robotomono/RobotoMono[wght].ttf")


def b64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def icon_uri(name: str) -> str:
    p = ICONS_DIR / f"{name}.svg"
    return f"data:image/svg+xml;base64,{b64_file(p)}"


# Official 4-color Google Cloud emblem (used ONLY on the outer "Google Cloud" zone header)
GCP_LOGO_SVG = """
<svg viewBox="0 0 24 20" width="22" height="18" xmlns="http://www.w3.org/2000/svg">
  <path fill="#EA4335" d="M15.2 5.6l2.1-2.1.1-.9C15.5 1 12.9.2 10.3.6 7.7 1 5.5 2.7 4.4 5.1l.8-.1 3-.5.2-.2c1.3-1.4 3.4-1.7 5.1-.7l1.7 2z"/>
  <path fill="#4285F4" d="M19.5 6.8c-.5-1.8-1.5-3.4-3-4.5l-2.8 2.8c1.2.9 1.9 2.3 1.9 3.8v.5c1.3 0 2.4 1.1 2.4 2.4s-1.1 2.4-2.4 2.4H12l-.5.5v3l.5.5h3.6c3.5 0 6.4-2.9 6.4-6.4 0-2.1-1-4.1-2.5-5z"/>
  <path fill="#34A853" d="M7.8 18.2h4.2v-4H7.8c-.6 0-1.1-.2-1.5-.5l-1 .3-2 2-.2.8c1.3 1 2.9 1.4 4.7 1.4z"/>
  <path fill="#FBBC05" d="M7.8 5.4C4.3 5.4 1.4 8.3 1.4 11.8c0 2 .9 3.8 2.4 5l2.8-2.8c-1.1-.5-1.6-1.8-1.1-2.9.5-1.1 1.8-1.6 2.9-1.1l2.8-2.8c-1-.7-2.2-1.8-3.4-1.8z"/>
</svg>
"""

MAT_USERS_SVG = """
<svg viewBox="0 0 24 24" width="28" height="28" fill="#616161" xmlns="http://www.w3.org/2000/svg">
  <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>
</svg>
"""

MAT_BRANCH_SVG = """
<svg viewBox="0 0 24 24" width="28" height="28" fill="#616161" xmlns="http://www.w3.org/2000/svg">
  <path d="M12 7V3H2v18h20V7H12zM6 19H4v-2h2v2zm0-4H4v-2h2v2zm0-4H4V9h2v2zm0-4H4V5h2v2zm4 12H8v-2h2v2zm0-4H8v-2h2v2zm0-4H8V9h2v2zm0-4H8V5h2v2zm10 12h-8v-2h2v-2h-2v-2h2v-2h-2V9h8v10zm-2-8h-2v2h2v-2zm0 4h-2v2h2v-2z"/>
</svg>
"""

MAT_LAPTOP_SVG = """
<svg viewBox="0 0 24 24" width="28" height="28" fill="#616161" xmlns="http://www.w3.org/2000/svg">
  <path d="M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/>
</svg>
"""

MAT_WARNING_USER_SVG = """
<svg viewBox="0 0 24 24" width="26" height="26" fill="#C62828" xmlns="http://www.w3.org/2000/svg">
  <path d="M12 5.99L19.53 19H4.47L12 5.99M12 2L1 21h22L12 2zm1 14h-2v2h2v-2zm0-6h-2v4h2v-4z"/>
</svg>
"""


class GCPDiagram:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.elements = []
        self.svg_paths = []
        self.badges = []

    def gcp_outer_zone(self, x: int, y: int, w: int, h: int, label_suffix: str = ""):
        suffix_html = (
            f'<span style="color:#616161;font-weight:400;font-size:12.5px;margin-left:10px;">{html.escape(label_suffix)}</span>'
            if label_suffix
            else ""
        )
        self.elements.append(f"""
        <div class="gcp-outer" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;">
          <div class="gcp-header">
            {GCP_LOGO_SVG}
            <span class="gcp-word-bold">Google</span><span class="gcp-word-reg">Cloud</span>
            {suffix_html}
          </div>
        </div>
        """)

    def zone(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        title: str,
        subtitle: str = "",
        kind: str = "project",
        icon: str = "",
        stacked: bool = False,
        dashed: bool = False,
        v_center: bool = False,
    ):
        palettes = {
            "project": ("#E1F5FE", "#B3E5FC", "#263238"),
            "subzone": ("#FFFFFF", "#CFD8DC", "#37474F"),
            "ingress": ("#EEF4FF", "#C5D9F8", "#263238"),
            "governance": ("#F3E5F5", "#D1C4E9", "#4A148C"),
            "onprem": ("#EFEBE9", "#D7CCC8", "#4E342E"),
            "users": ("#F1F8E9", "#C5E1A5", "#2E7D32"),
            "sand": ("#FFF8E1", "#FFE082", "#5D4037"),
            "neutral": ("#F6F6F6", "#DFDFDF", "#424242"),
        }
        bg, border, fg = palettes.get(kind, palettes["project"])
        border_style = "dashed" if dashed else "solid"
        if stacked:
            self.elements.append(
                f'<div style="position:absolute;left:{x+5}px;top:{y+5}px;width:{w}px;height:{h}px;'
                f'background:{bg};border:1px {border_style} {border};border-radius:2px;"></div>'
            )
        icon_html = (
            f'<img src="{icon_uri(icon)}" width="15" height="15" style="margin-right:6px;flex-shrink:0;" />'
            if icon
            else ""
        )
        sub_html = (
            f'<span style="font-weight:400;color:#616161;font-size:11px;margin-left:6px;white-space:nowrap;">{html.escape(subtitle)}</span>'
            if subtitle
            else ""
        )
        header_style = (
            "display:flex;align-items:center;height:100%;padding:0 12px;"
            if v_center
            else "display:flex;align-items:center;padding:7px 10px 0 10px;line-height:1.2;"
        )
        self.elements.append(f"""
        <div style="position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;
                    background:{bg};border:1px {border_style} {border};border-radius:2px;box-sizing:border-box;">
          <div style="{header_style}">
            {icon_html}
            <span style="font-size:12px;font-weight:500;color:{fg};white-space:nowrap;">{html.escape(title)}</span>
            {sub_html}
          </div>
        </div>
        """)

    def card(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        title: str,
        subtitle: str = "",
        icon: str = "",
        spec: str = "",
        stacked: bool = False,
        status: str = "normal",
    ):
        styles = {
            "normal": ("#FFFFFF", "#DDDDDD", "#212121", "#757575"),
            "active": ("#FFFFFF", "#90CAF9", "#1565C0", "#5F6368"),
            "healthy": ("#F1F8E9", "#81C784", "#1B5E20", "#2E7D32"),
            "error": ("#FFEBEE", "#E57373", "#B71C1C", "#C62828"),
        }
        bg, border, title_col, sub_col = styles.get(status, styles["normal"])
        if stacked:
            self.elements.append(
                f'<div class="gcp-card" style="left:{x+5}px;top:{y+5}px;width:{w}px;height:{h}px;'
                f'background:{bg};border-color:{border};"></div>'
            )
        icon_html = (
            f'<img src="{icon_uri(icon)}" width="26" height="26" style="flex-shrink:0;margin-right:9px;" />'
            if icon
            else ""
        )
        sub_html = (
            f'<div style="font-size:10.5px;color:{sub_col};line-height:1.22;margin-top:1px;white-space:nowrap;">{html.escape(subtitle)}</div>'
            if subtitle
            else ""
        )
        spec_html = (
            f'<div style="border-top:1px solid #EEEEEE;margin-top:4px;padding-top:3px;'
            f'font-size:9.8px;color:#616161;font-family:\'Roboto Mono\',monospace;line-height:1.2;white-space:nowrap;">{html.escape(spec)}</div>'
            if spec
            else ""
        )
        self.elements.append(f"""
        <div class="gcp-card" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;background:{bg};border-color:{border};">
          <div style="display:flex;align-items:center;height:100%;padding:0 9px;box-sizing:border-box;">
            {icon_html}
            <div style="min-width:0;flex:1;">
              <div style="font-size:11.5px;font-weight:500;color:{title_col};line-height:1.2;white-space:nowrap;">{html.escape(title)}</div>
              {sub_html}
              {spec_html}
            </div>
          </div>
        </div>
        """)

    def user_card(self, x: int, y: int, w: int, h: int, label: str, sub: str = "", svg_icon: str = MAT_USERS_SVG, status: str = "normal"):
        bg = "#FFEBEE" if status == "error" else "#FFFFFF"
        border = "#E57373" if status == "error" else "#DDDDDD"
        fg = "#B71C1C" if status == "error" else "#424242"
        sub_html = f'<div style="font-size:10px;color:#757575;margin-top:2px;white-space:nowrap;">{html.escape(sub)}</div>' if sub else ""
        self.elements.append(f"""
        <div class="gcp-card" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;background:{bg};border-color:{border};
                    display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:6px;">
          <div style="margin-bottom:4px;display:flex;align-items:center;justify-content:center;">{svg_icon}</div>
          <div style="font-size:11px;font-weight:500;color:{fg};line-height:1.2;white-space:nowrap;">{html.escape(label)}</div>
          {sub_html}
        </div>
        """)

    def text_note(self, x: int, y: int, w: int, text: str, align: str = "left", color: str = "#616161", size: float = 11.0, weight: int = 400):
        self.elements.append(
            f'<div style="position:absolute;left:{x}px;top:{y}px;width:{w}px;text-align:{align};'
            f'font-size:{size}px;font-weight:{weight};color:{color};line-height:1.35;">{html.escape(text)}</div>'
        )

    def divider_line(self, x1: int, y1: int, x2: int, y2: int, color: str = "#BDBDBD", dashed: bool = True, label: str = ""):
        dash = 'stroke-dasharray="4,4"' if dashed else ""
        self.svg_paths.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.2" {dash} />'
        )
        if label:
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2
            # Render in badges layer ABOVE svg.wires so the dashed line never cuts through text
            self.badges.append(
                f'<div style="position:absolute;left:{mx - 220}px;top:{my - 9}px;width:440px;text-align:center;">'
                f'<span style="background:#F6F6F6;padding:2px 10px;font-size:10.5px;color:#546E7A;font-weight:500;">'
                f'{html.escape(label)}</span></div>'
            )

    def path(
        self,
        points: list[tuple[int, int]],
        color: str = "#4284F3",
        dashed: bool = False,
        arrow: bool = True,
        start_dot: bool = False,
        width: float = 1.6,
    ):
        marker_map = {
            "#4284F3": "arr-blue",
            "#757575": "arr-grey",
            "#34A853": "arr-green",
            "#EA4335": "arr-red",
        }
        m_id = marker_map.get(color, "arr-blue")
        d = f"M {points[0][0]} {points[0][1]} " + " ".join(f"L {px} {py}" for px, py in points[1:])
        dash_attr = 'stroke-dasharray="4,3"' if dashed else ""
        marker_attr = f'marker-end="url(#{m_id})"' if arrow else ""
        self.svg_paths.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linejoin="round" {dash_attr} {marker_attr} />'
        )
        if start_dot:
            sx, sy = points[0]
            self.svg_paths.append(f'<circle cx="{sx}" cy="{sy}" r="2.6" fill="{color}" />')

    def step_badge(self, cx: int, cy: int, num: str, border_color: str = "#78909C"):
        # Rendered in .layer-badges (z-index 10) so wires never cross over the digit!
        self.badges.append(f"""
        <div style="position:absolute;left:{cx-10}px;top:{cy-10}px;width:20px;height:20px;border-radius:50%;
                    background:#FFFFFF;border:1.6px solid {border_color};box-sizing:border-box;
                    display:flex;align-items:center;justify-content:center;
                    font-size:11px;font-weight:700;color:#263238;box-shadow:0 1px 2px rgba(0,0,0,0.12);">
          {html.escape(str(num))}
        </div>
        """)

    def step_legend(self, x: int, y: int, w: int, steps: list[tuple[str, str]], cols: int = 2):
        col_w = w // cols
        rows = (len(steps) + cols - 1) // cols
        h = rows * 28 + 12
        cells_html = []
        for idx, (num, desc) in enumerate(steps):
            r = idx // cols
            c = idx % cols
            top_border = "border-top:1px solid #EEEEEE;" if r > 0 else ""
            cells_html.append(f"""
            <div style="position:absolute;left:{c * col_w + 12}px;top:{r * 28 + 6}px;width:{col_w - 24}px;height:28px;
                        display:flex;align-items:center;{top_border}">
              <span style="display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;
                           border-radius:50%;border:1.4px solid #90A4AE;font-size:10px;font-weight:700;
                           color:#263238;margin-right:8px;flex-shrink:0;">{html.escape(num)}</span>
              <span style="font-size:11px;color:#424242;line-height:1.25;white-space:nowrap;">{html.escape(desc)}</span>
            </div>
            """)
        self.elements.append(f"""
        <div style="position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;
                    background:#FFFFFF;border:1px solid #D0D0D0;border-radius:2px;box-sizing:border-box;">
          {''.join(cells_html)}
        </div>
        """)

    def render_png(self, out_png: Path):
        if FONT_ROBOTO.exists() and FONT_MONO.exists():
            font_css = f"""
  @font-face {{
    font-family: 'Roboto';
    src: url('file://{FONT_ROBOTO}') format('truetype');
    font-weight: 100 900;
    font-stretch: 75% 125%;
  }}
  @font-face {{
    font-family: 'Roboto Mono';
    src: url('file://{FONT_MONO}') format('truetype');
    font-weight: 100 700;
  }}"""
        else:
            font_css = "@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Roboto+Mono:wght@400;500&display=swap');"
        doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  {font_css}
  * {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    -webkit-font-smoothing: antialiased;
  }}
  html, body {{
    width: {self.width}px;
    height: {self.height}px;
    background: #FFFFFF;
    font-family: 'Roboto', sans-serif;
    overflow: hidden;
  }}
  .canvas {{
    position: relative;
    width: {self.width}px;
    height: {self.height}px;
    background: #FFFFFF;
  }}
  .gcp-outer {{
    position: absolute;
    background: #F6F6F6;
    border: 1px solid #DFDFDF;
    border-radius: 2px;
  }}
  .gcp-header {{
    display: flex;
    align-items: center;
    padding: 10px 14px;
  }}
  .gcp-word-bold {{
    font-size: 14px;
    font-weight: 500;
    color: #5F6368;
    margin-left: 7px;
  }}
  .gcp-word-reg {{
    font-size: 14px;
    font-weight: 400;
    color: #5F6368;
    margin-left: 4px;
  }}
  .gcp-card {{
    position: absolute;
    background: #FFFFFF;
    border: 1px solid #DDDDDD;
    border-radius: 2px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.10);
  }}
  .layer-nodes {{
    position: absolute;
    inset: 0;
    z-index: 2;
  }}
  svg.wires {{
    position: absolute;
    left: 0;
    top: 0;
    width: {self.width}px;
    height: {self.height}px;
    pointer-events: none;
    z-index: 5;
  }}
  .layer-badges {{
    position: absolute;
    inset: 0;
    z-index: 10;
    pointer-events: none;
  }}
</style>
</head>
<body>
  <div class="canvas">
    <div class="layer-nodes">
      {''.join(self.elements)}
    </div>
    <svg class="wires" viewBox="0 0 {self.width} {self.height}" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="arr-blue" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 1 L 7 4 L 0 7 z" fill="#4284F3"/>
        </marker>
        <marker id="arr-grey" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 1 L 7 4 L 0 7 z" fill="#757575"/>
        </marker>
        <marker id="arr-green" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 1 L 7 4 L 0 7 z" fill="#34A853"/>
        </marker>
        <marker id="arr-red" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 1 L 7 4 L 0 7 z" fill="#EA4335"/>
        </marker>
      </defs>
      {''.join(self.svg_paths)}
    </svg>
    <div class="layer-badges">
      {''.join(self.badges)}
    </div>
  </div>
</body>
</html>
"""
        html_path = out_png.with_suffix(".html")
        html_path.write_text(doc, encoding="utf-8")
        raw_png = out_png.with_name(out_png.stem + "_raw.png")
        subprocess.run(
            [
                "chromium",
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                f"--window-size={self.width},{self.height + 120}",
                f"--screenshot={raw_png}",
                f"file://{html_path}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        im = Image.open(raw_png)
        cropped = im.crop((0, 0, self.width * 2, self.height * 2))
        cropped.save(out_png, optimize=True)
        raw_png.unlink(missing_ok=True)


# ==============================================================================
# DIAGRAM 1: Cell-based reference architecture on Google Cloud
# ==============================================================================
def build_diagram_1(out_png: Path):
    d = GCPDiagram(1180, 650)

    # External left-side clients
    d.zone(20, 128, 126, 136, "Public clients", kind="users")
    d.user_card(35, 158, 96, 92, "SaaS & API", "X-Tenant-Id", MAT_USERS_SVG)

    d.zone(20, 366, 126, 140, "On-premises", kind="onprem")
    d.user_card(35, 396, 96, 96, "Enterprise", "Branch / VPN", MAT_BRANCH_SVG)

    # Outer Google Cloud container
    d.gcp_outer_zone(172, 20, 988, 536)

    # Top out-of-band governance zone
    d.zone(194, 58, 944, 84, "Project: platform-governance", "Out-of-band observability (no data-plane dependency)", kind="governance", icon="project")
    d.card(212, 86, 220, 46, "App Hub", "Models cell-01..cell-08", icon="app_hub")
    d.card(452, 86, 220, 46, "Cloud Hub", "Per-cell health & quotas", icon="cloud_hub")
    d.card(692, 86, 220, 46, "Cloud Monitoring", "Cell SLO burn alerts", icon="cloud_monitoring")
    d.text_note(928, 92, 196, "Control-plane issues never interrupt cell traffic", color="#4A148C", size=10.5, weight=500)

    # Left column inside Google Cloud: Thin stateless routing project
    d.zone(194, 158, 250, 378, "Project: ingress", "Stateless routing", kind="ingress", icon="project")

    # Public ingress cards
    d.card(212, 190, 214, 50, "Cloud DNS", "Global Anycast or geo DNS", icon="cloud_dns")
    d.card(212, 254, 214, 50, "Cloud Armor", "Per-tenant rate limiting", icon="cloud_armor")
    d.card(212, 318, 214, 66, "Cloud Load Balancing", "External ALB + Wasm", icon="cloud_load_balancing", spec="Stamps k-of-N cell hand", status="active")

    # Private Cloud WAN ingress card
    d.card(212, 412, 214, 68, "Cloud WAN (NCC hub)", "STAR topology spokes", icon="network_connectivity_center", spec="Blocks cross-cell routes")
    d.text_note(212, 494, 214, "Zero database lookups on hot path", align="center", color="#546E7A", size=10.5)

    # Right column inside Google Cloud: Isolated workload cells
    cell_x = 486
    cell_w = 652

    # Cell 01
    d.zone(cell_x, 158, cell_w, 108, "Project: saas-cell-01 (us-central1)", "Canary cell · Isolated quotas", kind="project", icon="project")
    d.card(504, 192, 192, 58, "Cloud Run / GKE", "Isolated cell NEGs", icon="cloud_run", spec="Stateless compute")
    d.card(720, 192, 192, 58, "Spanner", "Dedicated cell instance", icon="spanner", spec="No shared DB locks")
    d.card(936, 192, 184, 58, "VPC · IAM · KMS", "Spoke VPC & CMEK", icon="virtual_private_cloud", spec="Per-cell identity")

    # Bulkhead divider between Cell 01 and Cell 02
    d.divider_line(cell_x + 8, 279, cell_x + cell_w - 8, 279, color="#B0BEC5", dashed=True, label="Hard bulkhead: no east-west RPCs or shared state across cells")

    # Cell 02
    d.zone(cell_x, 292, cell_w, 108, "Project: saas-cell-02 (us-east4)", "Wave 1 cell · Isolated quotas", kind="project", icon="project")
    d.card(504, 326, 192, 58, "Cloud Run / GKE", "Isolated cell NEGs", icon="gke", spec="Stateless compute")
    d.card(720, 326, 192, 58, "Spanner", "Dedicated cell instance", icon="spanner", spec="No shared DB locks")
    d.card(936, 326, 184, 58, "VPC · IAM · KMS", "Spoke VPC & CMEK", icon="key_management_service", spec="Per-cell identity")

    # Cells 03 .. 08 (stacked doubleRect zone + stacked cards)
    d.zone(cell_x, 418, cell_w - 5, 112, "Projects: saas-cell-03 .. saas-cell-08", "6 additional cells (scale out by adding cells)", kind="project", icon="project", stacked=True)
    d.card(504, 452, 188, 58, "Cloud Run / GKE", "Isolated cell NEGs", icon="cloud_run", spec="Cells 03..08", stacked=True)
    d.card(720, 452, 188, 58, "Spanner", "Dedicated instances", icon="spanner", spec="1 instance per cell", stacked=True)
    d.card(936, 452, 179, 58, "VPC · IAM · KMS", "Isolated spokes", icon="identity_and_access_management", spec="Independent quotas", stacked=True)

    # Orthogonal connectors
    d.path([(131, 204), (162, 204), (162, 279), (212, 279)], color="#4284F3", start_dot=True)
    d.path([(319, 240), (319, 254)], color="#4284F3")
    d.path([(319, 304), (319, 318)], color="#4284F3")

    # ALB -> Cell 01, Cell 02, Cells 03..08
    d.path([(426, 351), (460, 351), (460, 221), (504, 221)], color="#4284F3", start_dot=True)
    d.path([(460, 351), (504, 351)], color="#4284F3")
    d.path([(460, 351), (460, 481), (504, 481)], color="#4284F3")

    # Inside cells: Compute -> Spanner
    d.path([(696, 221), (720, 221)], color="#4284F3")
    d.path([(696, 355), (720, 355)], color="#4284F3")
    d.path([(692, 481), (720, 481)], color="#4284F3")

    # Enterprise branch -> Cloud WAN (pure horizontal orthogonal!) -> Cell spokes
    d.path([(131, 446), (212, 446)], color="#757575", start_dot=True)
    d.path([(426, 446), (474, 446), (474, 236), (504, 236)], color="#757575", dashed=True)
    d.path([(474, 368), (504, 368)], color="#757575", dashed=True)
    d.path([(474, 446), (474, 495), (504, 495)], color="#757575", dashed=True)

    # Telemetry upward to governance plane
    d.path([(802, 158), (802, 132)], color="#757575", dashed=True)

    # Step badges (rendered cleanly above lines)
    d.step_badge(162, 241, "1", "#4284F3")
    d.step_badge(460, 304, "2", "#4284F3")
    d.step_badge(164, 446, "3", "#757575")
    d.step_badge(802, 145, "4", "#757575")

    d.step_legend(
        20,
        568,
        1140,
        [
            ("1", "Public requests pass through Cloud DNS and Cloud Armor per-tenant rate limits."),
            ("2", "External ALB runs a stateless Wasm router to assign each tenant to isolated cells."),
            ("3", "Cloud WAN (NCC STAR) connects enterprise branches while blocking cell-to-cell traffic."),
            ("4", "App Hub, Cloud Hub, and Cloud Monitoring track cell SLOs without sharing a data plane."),
        ],
        cols=2,
    )

    d.render_png(out_png)


# ==============================================================================
# DIAGRAM 2: Standard cell sharding vs. shuffle sharding ("hand of cards")
# ==============================================================================
def build_diagram_2(out_png: Path):
    d = GCPDiagram(1180, 590)

    # ---------------- LEFT PANEL: Standard Sharding (N=8, k=1) ----------------
    d.gcp_outer_zone(20, 20, 558, 480, "A. Standard cell sharding (1 cell per tenant)")

    d.user_card(38, 86, 118, 78, "Tenant X", "Poison-pill request", MAT_WARNING_USER_SVG, status="error")
    d.user_card(38, 196, 118, 78, "Alice", "Assigned: cell-02", MAT_USERS_SVG, status="error")
    d.user_card(38, 306, 118, 78, "Bob", "Assigned: cell-04", MAT_USERS_SVG)

    d.card(192, 198, 154, 72, "External ALB", "1-to-1 cell router", icon="cloud_load_balancing", spec="k = 1 (no backup)")

    d.zone(380, 66, 180, 368, "8 isolated cells", kind="project")
    d.card(394, 98, 152, 50, "cell-01", "200 OK · Healthy", icon="cloud_run")
    d.card(394, 160, 152, 54, "cell-02", "503 Crashed by X", icon="cloud_run", status="error")
    d.card(394, 226, 152, 50, "cell-03", "200 OK · Healthy", icon="cloud_run")
    d.card(394, 288, 152, 50, "cell-04", "200 OK · Bob OK", icon="cloud_run", status="healthy")
    d.card(394, 350, 147, 54, "cell-05 .. cell-08", "4 healthy cells", icon="cloud_run", stacked=True)

    d.path([(156, 125), (174, 125), (174, 220), (192, 220)], color="#EA4335", start_dot=True)
    d.path([(156, 235), (192, 235)], color="#EA4335", dashed=True, start_dot=True)
    d.path([(156, 345), (174, 345), (174, 250), (192, 250)], color="#34A853", start_dot=True)

    d.path([(346, 224), (366, 224), (366, 187), (394, 187)], color="#EA4335")
    d.path([(346, 248), (366, 248), (366, 313), (394, 313)], color="#34A853")
    d.step_badge(174, 172, "1", "#EA4335")

    d.zone(38, 446, 522, 38, "Blast radius: 1 / 8 = 12.5% of all tenants go offline (Alice shares fate with Tenant X)", kind="subzone", v_center=True)

    # ---------------- RIGHT PANEL: Shuffle Sharding (N=8, k=2/3) ----------------
    d.gcp_outer_zone(602, 20, 558, 480, "B. Shuffle sharding (virtual hand of k cells per tenant)")

    d.user_card(620, 86, 124, 78, "Tenant X", "Hand: [cell-02, 05]", MAT_WARNING_USER_SVG, status="error")
    d.user_card(620, 196, 124, 78, "Alice", "Hand: [cell-02, 04]", MAT_USERS_SVG)
    d.user_card(620, 306, 124, 78, "Bob", "Hand: [cell-05, 07]", MAT_USERS_SVG)

    d.card(780, 198, 156, 72, "External ALB", "Proxy-Wasm + retry", icon="cloud_load_balancing", spec="Auto-retries hand", status="active")

    d.zone(972, 66, 172, 368, "Same 8 cells", kind="project")
    d.card(986, 96, 144, 44, "cell-01", "200 OK · Healthy", icon="cloud_run")
    d.card(986, 148, 144, 46, "cell-02", "503 Crashed (X)", icon="cloud_run", status="error")
    d.card(986, 202, 144, 46, "cell-04", "200 OK · Alice!", icon="cloud_run", status="healthy")
    d.card(986, 256, 144, 46, "cell-05", "503 Crashed (X)", icon="gke", status="error")
    d.card(986, 310, 144, 46, "cell-07", "200 OK · Bob!", icon="gke", status="healthy")
    d.card(986, 364, 139, 48, "cell-03, 06, 08", "3 healthy cells", icon="cloud_run", stacked=True)

    d.path([(744, 125), (762, 125), (762, 218), (780, 218)], color="#EA4335", start_dot=True)
    d.path([(744, 235), (780, 235)], color="#4284F3", start_dot=True)
    d.path([(744, 345), (762, 345), (762, 252), (780, 252)], color="#4284F3", start_dot=True)

    # Poisoned paths from ALB (dashed red) + instant failover paths (solid green)
    d.path([(936, 216), (952, 216), (952, 171), (986, 171)], color="#EA4335", dashed=True)
    d.path([(952, 216), (952, 279), (986, 279)], color="#EA4335", dashed=True)

    d.path([(936, 234), (966, 234), (966, 225), (986, 225)], color="#34A853")
    d.path([(936, 252), (964, 252), (964, 333), (986, 333)], color="#34A853")

    d.step_badge(762, 172, "2", "#EA4335")
    d.step_badge(966, 225, "3", "#34A853")
    d.step_badge(964, 306, "4", "#34A853")

    d.zone(620, 446, 524, 38, "With 3-cell hands across 8 cells (56 shards), 98.2% of tenants stay online (<2% impact)", kind="users", v_center=True)

    d.step_legend(
        20,
        514,
        1140,
        [
            ("1", "In standard sharding, Tenant X crashes cell-02 and takes down every neighbor on that cell (12.5%)."),
            ("2", "In shuffle sharding, each tenant gets a unique 2- or 3-cell combination from the same 8 cells."),
            ("3", "Alice shares cell-02 with Tenant X, so the load balancer automatically retries her backup (cell-04)."),
            ("4", "Bob fails over to cell-07 with 200 OK; only a tenant with the exact same hand (<2%) is affected."),
        ],
        cols=2,
    )

    d.render_png(out_png)


# ==============================================================================
# DIAGRAM 3: Global Anycast vs. regional external Application Load Balancers
# ==============================================================================
def build_diagram_3(out_png: Path):
    d = GCPDiagram(1180, 580)

    # ---------------- LEFT PANEL: Pattern A (Global Anycast ALB) ----------------
    d.gcp_outer_zone(20, 20, 558, 470, "Option A: Global external Application Load Balancer")

    d.user_card(38, 76, 98, 84, "Global SaaS", "API users", MAT_USERS_SVG)
    d.card(166, 86, 176, 62, "Cloud Armor", "Global edge WAF", icon="cloud_armor", spec="DDoS & rate limit")
    d.card(368, 82, 190, 70, "Global external ALB", "Single Anycast VIP", icon="cloud_load_balancing", spec="Proxy-Wasm cell stamp", status="active")

    # Two regional cell zones under Global ALB (arrows enter right of header text!)
    d.zone(38, 202, 250, 220, "us-central1", "cell-01", kind="project", icon="project")
    d.card(56, 238, 214, 56, "Cloud Run / GKE", "Serverless or zonal NEG", icon="cloud_run", spec="cell-01 compute")
    d.card(56, 310, 214, 56, "Spanner", "Regional instance", icon="spanner", spec="cell-01 database")
    d.text_note(56, 382, 214, "Outlier ejection < 5s", align="center", color="#1565C0", size=10.5, weight=500)

    d.zone(308, 202, 250, 220, "us-east4", "cell-02", kind="project", icon="project")
    d.card(326, 238, 214, 56, "Cloud Run / GKE", "Serverless or zonal NEG", icon="gke", spec="cell-02 compute")
    d.card(326, 310, 214, 56, "Spanner", "Regional instance", icon="spanner", spec="cell-02 database")
    d.text_note(326, 382, 214, "Instant cross-region retry", align="center", color="#1565C0", size=10.5, weight=500)

    # Connectors Option A (enter at x=215 and x=485 so they never cross zone titles!)
    d.path([(136, 117), (166, 117)], color="#4284F3", start_dot=True)
    d.path([(342, 117), (368, 117)], color="#4284F3")
    d.path([(463, 152), (463, 178), (215, 178), (215, 238)], color="#4284F3")
    d.path([(463, 178), (485, 178), (485, 238)], color="#4284F3")
    d.path([(163, 294), (163, 310)], color="#4284F3")
    d.path([(433, 294), (433, 310)], color="#4284F3")
    d.step_badge(350, 178, "1", "#4284F3")
    d.step_badge(270, 202, "4", "#4284F3")

    d.zone(38, 436, 520, 38, "Best for global SaaS & AI APIs · Validate shared URL map in CI before rollout", kind="sand", v_center=True)

    # ---------------- RIGHT PANEL: Pattern B (Regional External ALBs) ----------------
    d.gcp_outer_zone(602, 20, 558, 470, "Option B: Regional external Application Load Balancers")

    d.user_card(620, 74, 98, 80, "Regulated", "Regional users", MAT_BRANCH_SVG)
    d.card(748, 80, 186, 62, "Cloud DNS", "Geo / residency routing", icon="cloud_dns", spec="Jurisdiction VIPs")
    d.card(954, 80, 188, 62, "Cloud WAN", "Regional NCC spokes", icon="network_connectivity_center", spec="Private branch spokes")

    # Sovereign region 1
    d.zone(620, 180, 246, 242, "us-central1", "US cell", kind="project", icon="project")
    d.card(636, 214, 214, 52, "Regional external ALB", "Regional Envoy subnet", icon="cloud_load_balancing", status="active")
    d.card(636, 278, 214, 52, "Cloud Run / GKE", "Regional cell NEG", icon="cloud_run")
    d.card(636, 342, 214, 52, "Regional Spanner", "US CMEK residency", icon="spanner")

    # Vertical dashed residency boundary
    d.divider_line(881, 182, 881, 420, color="#E57373", dashed=True)

    # Sovereign region 2
    d.zone(896, 180, 246, 242, "europe-west1", "EU cell", kind="project", icon="project")
    d.card(912, 214, 214, 52, "Regional external ALB", "Regional Envoy subnet", icon="cloud_load_balancing", status="active")
    d.card(912, 278, 214, 52, "Cloud Run / GKE", "Regional cell NEG", icon="gke")
    d.card(912, 342, 214, 52, "Regional Spanner", "EU CMEK residency", icon="spanner")

    # Connectors Option B (enter at x=818 and x=1094 clear of zone header titles!)
    d.path([(718, 112), (748, 112)], color="#4284F3", start_dot=True)
    d.path([(841, 142), (841, 160), (818, 160), (818, 214)], color="#4284F3")
    d.path([(841, 160), (1086, 160), (1086, 214)], color="#4284F3")
    # Cloud WAN private spoke connectors
    d.path([(1048, 142), (1048, 168), (834, 168), (834, 214)], color="#757575", dashed=True)
    d.path([(1048, 168), (1104, 168), (1104, 214)], color="#757575", dashed=True)

    d.path([(743, 266), (743, 278)], color="#4284F3")
    d.path([(743, 330), (743, 342)], color="#4284F3")
    d.path([(1019, 266), (1019, 278)], color="#4284F3")
    d.path([(1019, 330), (1019, 342)], color="#4284F3")
    d.step_badge(920, 160, "2", "#4284F3")
    d.step_badge(881, 304, "3", "#EA4335")

    d.zone(620, 436, 522, 38, "Best for banking & healthcare residency · Regional failover via Cloud DNS (30-60s TTL)", kind="users", v_center=True)

    d.step_legend(
        20,
        504,
        1140,
        [
            ("1", "Option A terminates TLS at the closest Google edge PoP and fails over across cells in under 5s."),
            ("2", "Option B keeps Envoy proxies, URL maps, Cloud Armor, and Spanner 100% inside each region."),
            ("3", "Dashed red line denotes strict regional control-plane and data-residency separation."),
            ("4", "Both options keep backend cells isolated in separate Google Cloud projects."),
        ],
        cols=2,
    )

    d.render_png(out_png)


# ==============================================================================
# DIAGRAM 4: Progressive wave-by-wave delivery in Cloud Deploy
# ==============================================================================
def build_diagram_4(out_png: Path):
    d = GCPDiagram(1180, 572)

    # External developer card on left
    d.zone(20, 126, 114, 146, "Development", kind="governance")
    d.user_card(31, 158, 92, 96, "Platform team", "Git commit", MAT_LAPTOP_SVG)

    # Google Cloud outer container
    d.gcp_outer_zone(158, 20, 1002, 460)

    # Left CI/CD column inside Google Cloud
    d.zone(176, 62, 200, 304, "Release pipeline", kind="ingress", icon="cloud_deploy")
    d.card(188, 96, 176, 56, "Cloud Build", "Builds image & Wasm", icon="cloud_build")
    d.card(188, 172, 176, 56, "Artifact Registry", "Immutable SHA digest", icon="artifact_registry")
    d.card(188, 248, 176, 68, "Cloud Deploy", "Serial wave pipeline", icon="cloud_deploy", spec="Weekend freeze rule", status="active")
    d.text_note(184, 328, 184, "Blocks Fri-Sun multi-cell waves", align="center", color="#B71C1C", size=10.5, weight=500)

    # Four progressive cell wave columns (generous 176px width -> zero 2-line wrap!)
    waves = [
        (402, "Wave 0: Canary", "1 cell (12.5%)", "cell-00", "us-central1", False, "15m bake timer"),
        (590, "Wave 1: Prod", "1 cell (12.5%)", "cell-01", "us-east4", False, "5xx burn < 0.5%"),
        (778, "Wave 2: Ring 2", "2 cells (25%)", "cell-02, cell-03", "Multi-target", True, "Approval gate"),
        (966, "Wave 3: Fleet", "4 cells (50%)", "cell-04 .. cell-07", "Multi-target", True, "100% converged"),
    ]
    for wx, wtitle, wsub, cname, creg, is_stack, vspec in waves:
        d.zone(wx, 62, 174, 304, wtitle, kind="project")
        d.text_note(wx + 10, 84, 154, wsub, color="#546E7A", size=10.5)
        c_w = 148 if is_stack else 152
        d.card(wx + 11, 110, c_w, 64, cname, creg, icon="cloud_run" if not is_stack else "gke", spec="Isolated project", stacked=is_stack)
        d.card(wx + 11, 194, 152, 62, "SLO verify job", "Cloud Monitoring", icon="cloud_monitoring", spec=vspec)
        d.card(wx + 11, 274, 152, 56, "Auto-rollback", "repairRolloutRule", status="healthy" if wx == 966 else "normal", spec="Halts wave on 5xx")

    # Bottom governance strip inside Google Cloud (spacious height so header never touches cards!)
    d.zone(176, 380, 964, 82, "Application-centric release visibility", "Out-of-band rollout & SLO correlation", kind="governance")
    d.card(194, 408, 248, 44, "App Hub", "Maps rollouts to cell-00..07", icon="app_hub")
    d.card(460, 408, 258, 44, "Cloud Hub", "Correlates deploys & incidents", icon="cloud_hub")
    d.text_note(738, 411, 386, "If Wave 0 fails verification, Cloud Deploy rolls back cell-00 automatically — 87.5% of cells never receive the bad release.", color="#37474F", size=11, weight=500)

    # Connectors
    d.path([(123, 206), (146, 206), (146, 124), (188, 124)], color="#4284F3", start_dot=True)
    d.path([(276, 152), (276, 172)], color="#4284F3")
    d.path([(276, 228), (276, 248)], color="#4284F3")

    # Cloud Deploy -> Wave 0 -> Wave 1 -> Wave 2 -> Wave 3
    d.path([(364, 282), (388, 282), (388, 142), (413, 142)], color="#4284F3", start_dot=True)
    d.path([(565, 142), (601, 142)], color="#34A853")
    d.path([(753, 142), (789, 142)], color="#34A853")
    d.path([(937, 142), (977, 142)], color="#34A853")

    # Verify links inside waves
    for wx, *_ in waves:
        d.path([(wx + 87, 174), (wx + 87, 194)], color="#757575", dashed=True)
        d.path([(wx + 87, 256), (wx + 87, 274)], color="#757575", dashed=True)

    d.step_badge(146, 166, "1", "#4284F3")
    d.step_badge(388, 212, "2", "#4284F3")
    d.step_badge(580, 142, "3", "#34A853")
    d.step_badge(864, 380, "4", "#757575")

    d.step_legend(
        20,
        494,
        1140,
        [
            ("1", "Git push triggers Cloud Build to package immutable container and Wasm artifacts."),
            ("2", "Cloud Deploy rolls out to Wave 0 (cell-00 canary) first and runs synthetic SLO checks."),
            ("3", "Releases promote across Wave 1, Wave 2, and Wave 3 only after bake timers and gates pass."),
            ("4", "App Hub and Cloud Hub show live rollout status alongside per-cell error budgets."),
        ],
        cols=2,
    )

    d.render_png(out_png)


def main():
    targets = [
        ("01_cell_reference_architecture.png", build_diagram_1),
        ("02_shuffle_sharding_combinatorics.png", build_diagram_2),
        ("03_global_vs_regional_alb_ingress.png", build_diagram_3),
        ("04_cloud_deploy_wave_pipeline.png", build_diagram_4),
    ]
    for fname, fn in targets:
        out = HERE / fname
        print(f"Rendering {out}...")
        fn(out)
        print(f"  -> Saved {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
