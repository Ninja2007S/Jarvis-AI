"""Neural visualizations for the JARVIS HUD.

Two CPU-rendered scenes (QPainter only: no OpenGL, no shaders) live here:

  * Visualization 1, "Synapse Field": a wide, branching, multicolour network.
    Fractal arbors grow from a white-hot core out to seven coloured regions;
    waves of light travel along every branch and long gradient axons link the
    regions together.
  * Visualization 2, "Neural Orb": a tighter field gathered around a luminous,
    rotating glass globe whose mesh fires in rolling waves, wrapped in orbital
    rings and a swarm of orbiting particles.

HudCanvas mixes this class in. Everything expensive (layouts, branching trees,
glow sprites) is built once in ``_build_neural_assets``; the per-frame painters
only project, bucket and batch paths so the raster-call count stays small.
Light is additive (CompositionMode_Plus), so overlapping colours bloom towards
white the way real light does.

If anything in the new renderer ever raises, ``_paint_neural_network`` prints
the traceback once and drops to a minimal glow instead of killing the app.
"""
from __future__ import annotations

import math
import random
import sys
import traceback
from types import SimpleNamespace

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath,
    QPen, QRadialGradient,
)

TAU = math.tau
PLUS = QPainter.CompositionMode.CompositionMode_Plus
OVER = QPainter.CompositionMode.CompositionMode_SourceOver
SOLID = Qt.PenStyle.SolidLine
ROUND = Qt.PenCapStyle.RoundCap
ROUNDJ = Qt.PenJoinStyle.RoundJoin
NOPEN = Qt.PenStyle.NoPen
NOBRUSH = Qt.BrushStyle.NoBrush
WHITE = (255, 255, 255)

# Region titles. The number is the palette slot, so a tag always matches the
# colour of the zone it labels.
REGIONS = (
    ("PRE-FRONTAL", 0), ("MOTOR CORTEX", 1), ("ASSOCIATION", 6),
    ("CONCEPT LAYER", 2), ("SENSORY CORTEX", 3), ("LANGUAGE", 4),
    ("HIPPOCAMPUS", 5),
)
BANDS = 12          # spectral bands used to batch the globe mesh
RAMP_N = 48         # resolution of the smooth spectral ramp


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def _rgba(rgb, alpha):
    a = 0 if alpha < 0 else (255 if alpha > 255 else int(alpha))
    return QColor(rgb[0], rgb[1], rgb[2], a)


def _mix(rgb, other, t):
    return (int(rgb[0] + (other[0] - rgb[0]) * t),
            int(rgb[1] + (other[1] - rgb[1]) * t),
            int(rgb[2] + (other[2] - rgb[2]) * t))


def _dots() -> QPainterPath:
    """A path for many small filled circles. Winding fill keeps overlapping
    dots solid instead of punching even-odd holes where they touch."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    return path


def _bucket(store: dict, key, factory=QPainterPath):
    path = store.get(key)
    if path is None:
        path = store[key] = factory()
    return path


# --------------------------------------------------------------------------
# Layout generators (run once)
# --------------------------------------------------------------------------
def _grow(nodes, root, trunks, max_depth, spread, decay, rng, lim=(1.12, 1.08)):
    """Grow a fractal arbor from ``nodes[root]``.

    ``nodes`` rows are [x, y, parent, depth, colour, seed, path_distance].
    ``trunks`` is a list of (heading, length, colour). A parent is always
    appended before its children, which the painter relies on.
    """
    rx, ry, rdist = nodes[root][0], nodes[root][1], nodes[root][6]
    stack = [(root, rx, ry, h, ln, 0, col, rdist) for h, ln, col in trunks]
    while stack:
        parent, x, y, heading, length, depth, col, dist = stack.pop()
        ex = _clamp(x + math.cos(heading) * length, -lim[0], lim[0])
        ey = _clamp(y + math.sin(heading) * length, -lim[1], lim[1])
        run = math.hypot(ex - x, ey - y)
        nodes.append([ex, ey, parent, depth, col, rng.random() * TAU, dist + run])
        idx = len(nodes) - 1
        if depth >= max_depth:
            continue
        kids = 2 if rng.random() < 0.78 else 3
        if depth == max_depth - 1 and rng.random() < 0.30:
            kids = 1
        for k in range(kids):
            turn = (k - (kids - 1) / 2.0) * spread + rng.uniform(-0.26, 0.26)
            stack.append((idx, ex, ey, heading + turn,
                          length * rng.uniform(decay - 0.08, decay + 0.08),
                          depth + 1, col, dist + run))


def _pack_tree(nodes):
    """Flatten tree rows into parallel lists (fast to index in the paint loop)
    and normalise path distance to 0..1 so a travelling wave can sweep it."""
    top = max(n[6] for n in nodes) or 1.0
    parents = {n[2] for n in nodes if n[2] >= 0}
    return {
        "x": [n[0] for n in nodes], "y": [n[1] for n in nodes],
        "par": [n[2] for n in nodes], "dep": [n[3] for n in nodes],
        "col": [n[4] for n in nodes], "seed": [n[5] for n in nodes],
        "dist": [n[6] / top for n in nodes],
        "leaf": [(i not in parents and nodes[i][2] >= 0) for i in range(len(nodes))],
    }


def _scatter(rng, clusters, lim):
    """Gaussian clouds of neurons. Returns particles, members and centres."""
    particles, members, centers = [], [], []
    for ci, (cx, cy, sx, sy, col, count) in enumerate(clusters):
        mem = []
        for _ in range(count):
            x = _clamp(rng.gauss(cx, sx), -lim[0], lim[0])
            y = _clamp(rng.gauss(cy, sy), -lim[1], lim[1])
            radius = rng.choices((0.55, 0.8, 1.1, 1.6, 2.4), (18, 43, 30, 6, 3))[0]
            mem.append(len(particles))
            particles.append((x, y, radius, col, rng.random() * TAU,
                              rng.uniform(0.004, 0.020), ci))
        members.append(mem)
        centers.append((cx, cy, col))
    return particles, members, centers


def _link(rng, particles, members, local, reach2, cross):
    """Edge rows: (a, b, src_cluster, phase_offset, direction, dst_cluster).
    Rows whose dst differs from src are long inter-region axons."""
    edges = []
    for ci, mem in enumerate(members):
        for _ in range(local):
            a = mem[rng.randrange(len(mem))]
            ax, ay = particles[a][0], particles[a][1]
            near = [b for b in mem if b != a
                    and (particles[b][0] - ax) ** 2 + (particles[b][1] - ay) ** 2 < reach2]
            if near:
                edges.append((a, rng.choice(near), ci, rng.random() * TAU,
                              rng.choice((-1.0, 1.0)), ci))
    for ci, mem in enumerate(members):
        for hop, count in cross:
            tj = (ci + hop) % len(members)
            for _ in range(count):
                edges.append((rng.choice(mem), rng.choice(members[tj]), ci,
                              rng.random() * TAU, rng.choice((-1.0, 1.0)), tj))
    return edges


def _stars(rng, count):
    out = []
    for _ in range(count):
        layer = rng.choice((0, 1, 1, 2))
        out.append((rng.uniform(-1.04, 1.04), rng.uniform(-1.04, 1.04),
                    rng.uniform(0.35, 1.05) * (0.8 + 0.25 * layer),
                    rng.random() * TAU, layer,
                    rng.choice((7, 7, 7, 0, 1, 2, 3, 4, 5, 6))))
    return out


class NeuralVizMixin:
    """Mixed into HudCanvas. Expects the attributes HudCanvas already owns:
    _neural_palette, _neural_energy, _neural_flash, _neural_phase,
    _neural_call_count, _amp_disp, speaking, state."""

    # ------------------------------------------------------------- assets
    def _build_neural_assets(self) -> None:
        rng = random.Random(7319)
        pal = [(c.red(), c.green(), c.blue()) for c in self._neural_palette]
        self._neural_rgb = pal

        # One smooth trip around the spectrum, used for shimmering hues.
        ramp = []
        for i in range(RAMP_N):
            c = QColor.fromHsvF((0.76 + i / RAMP_N) % 1.0, 0.78, 1.0)
            ramp.append((c.red(), c.green(), c.blue()))
        self._neural_ramp = ramp

        def glow(rgb, size, peak):
            img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            g = QPainter(img)
            g.setRenderHint(QPainter.RenderHint.Antialiasing)
            half = size / 2.0
            grad = QRadialGradient(half, half, half - 1.0)
            hot, warm = _mix(rgb, WHITE, 0.55), _mix(rgb, WHITE, 0.22)
            grad.setColorAt(0.00, _rgba(hot, peak))
            grad.setColorAt(0.08, _rgba(hot, peak * 0.82))
            grad.setColorAt(0.20, _rgba(warm, peak * 0.50))
            grad.setColorAt(0.42, _rgba(rgb, peak * 0.20))
            grad.setColorAt(0.70, _rgba(rgb, peak * 0.055))
            grad.setColorAt(1.00, _rgba(rgb, 0))
            g.setPen(NOPEN)
            g.setBrush(QBrush(grad))
            g.drawEllipse(QRectF(1, 1, size - 2, size - 2))
            g.end()
            return img

        def flare(rgb, size=96):
            img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            g = QPainter(img)
            g.setRenderHint(QPainter.RenderHint.Antialiasing)
            g.translate(size / 2.0, size / 2.0)
            tip = _mix(rgb, WHITE, 0.65)
            for angle, span, thick, peak in ((0, 1.0, 2.4, 235), (90, 1.0, 2.4, 235),
                                             (45, 0.58, 1.5, 125), (135, 0.58, 1.5, 125)):
                g.save()
                g.rotate(angle)
                h = size / 2.0 * span
                grad = QLinearGradient(-h, 0.0, h, 0.0)
                grad.setColorAt(0.0, _rgba(rgb, 0))
                grad.setColorAt(0.5, _rgba(tip, peak))
                grad.setColorAt(1.0, _rgba(rgb, 0))
                g.setPen(NOPEN)
                g.setBrush(QBrush(grad))
                g.drawRoundedRect(QRectF(-h, -thick / 2.0, h * 2.0, thick),
                                  thick / 2.0, thick / 2.0)
                g.restore()
            g.end()
            return img

        self._neural_glows = {i: glow(rgb, 64, 255 if i == 7 else 205)
                              for i, rgb in enumerate(pal)}
        self._neural_band_glows = [glow(ramp[i * (RAMP_N // BANDS)], 48, 200)
                                   for i in range(BANDS)]
        self._neural_flares = {i: flare(rgb) for i, rgb in enumerate(pal)}
        self._neural_bg = {}
        self._neural_failed = False
        self._neural_layouts = {False: self._layout_synapse(rng),
                                True: self._layout_orb(rng)}
        self._build_orb_assets(rng)

    def _layout_synapse(self, rng) -> dict:
        """Visualization 1: wide field, seven regions, trunks from the core."""
        clusters = (
            (-0.36, -0.50, 0.18, 0.17, 0, 84), (0.28, -0.42, 0.20, 0.19, 1, 88),
            (-0.58, -0.12, 0.18, 0.20, 2, 96), (0.58, -0.05, 0.19, 0.21, 3, 96),
            (0.28, 0.28, 0.20, 0.19, 4, 84), (-0.05, 0.58, 0.20, 0.16, 5, 96),
            (-0.62, 0.28, 0.16, 0.19, 6, 76),
        )
        particles, members, centers = _scatter(rng, clusters, (0.98, 0.98))
        edges = _link(rng, particles, members, 30, 0.075, ((3, 2), (2, 1)))

        core = (-0.14, -0.015)
        nodes = [[core[0], core[1], -1, -1, 7, 0.0, 0.0]]
        trunks = []
        for cx, cy, col in centers:
            h = math.atan2(cy - core[1], cx - core[0]) + rng.uniform(-0.12, 0.12)
            d = math.hypot(cx - core[0], cy - core[1])
            trunks.append((h, d * rng.uniform(0.48, 0.56), col))
        _grow(nodes, 0, trunks, 4, 0.60, 0.74, rng)
        # A few long fibres that run far past the regions, toward the edges.
        _grow(nodes, 0, [(rng.uniform(0, TAU), rng.uniform(0.34, 0.50), rng.randrange(7))
                         for _ in range(5)], 3, 0.66, 0.78, rng)
        for cx, cy, col in centers:
            soma = len(nodes)
            nodes.append([cx, cy, -1, -1, col, 0.0, math.hypot(cx - core[0], cy - core[1])])
            _grow(nodes, soma, [(rng.uniform(0, TAU), rng.uniform(0.09, 0.15), col)
                                for _ in range(4)], 2, 0.70, 0.72, rng)

        dust = [(rng.uniform(-1.05, 1.05), rng.uniform(-1.05, 1.05),
                 rng.uniform(0.5, 1.3), rng.randrange(7), rng.random() * TAU)
                for _ in range(110)]
        nebula = [(cx, cy, 0.66, col, 1.0) for cx, cy, col in centers]
        nebula += [(core[0], core[1], 1.15, 6, 1.5), (core[0], core[1], 0.55, 0, 1.2)]
        return {
            "particles": particles, "members": members, "centers": centers,
            "ccol": [c[2] for c in centers], "edges": edges,
            "tree": _pack_tree(nodes), "dust": dust, "stars": _stars(rng, 170),
            "nebula": nebula, "core": core, "sway": 1.0, "dscale": 1.0,
            "bend": 1.0, "fil": 34,
        }

    def _layout_orb(self, rng) -> dict:
        """Visualization 2: seven regions on a ring around the globe. Units are
        globe radii, so the field stays focused at any window size."""
        order = (0, 1, 4, 2, 5, 3, 6)       # violet > pink > red > amber > lime > cyan > indigo
        clusters = []
        for k, col in enumerate(order):
            ang = -math.pi / 2 + TAU * k / 7 + rng.uniform(-0.09, 0.09)
            rr = rng.uniform(0.94, 1.06)
            clusters.append((math.cos(ang) * 2.75 * rr, math.sin(ang) * 2.10 * rr,
                             0.60, 0.50, col, 60))
        lim = (3.9, 2.9)
        particles, members, centers = _scatter(rng, clusters, lim)
        edges = _link(rng, particles, members, 26, 0.70, ((1, 2), (3, 1)))

        nodes = []
        for cx, cy, col in centers:
            ang = math.atan2(cy, cx)
            root = len(nodes)
            nodes.append([math.cos(ang) * 1.12, math.sin(ang) * 1.12, -1, -1, col, 0.0, 0.0])
            d = math.hypot(cx, cy) - 1.12
            _grow(nodes, root, [(ang + rng.uniform(-0.1, 0.1), d * rng.uniform(0.52, 0.60), col)],
                  3, 0.62, 0.74, rng, lim)
        for _ in range(8):                  # short sprays straight off the surface
            ang, col = rng.uniform(0, TAU), rng.randrange(7)
            root = len(nodes)
            nodes.append([math.cos(ang) * 1.12, math.sin(ang) * 1.12, -1, -1, col, 0.0, 0.0])
            _grow(nodes, root, [(ang + rng.uniform(-0.3, 0.3), rng.uniform(0.35, 0.60), col)],
                  2, 0.75, 0.74, rng, lim)
        for cx, cy, col in centers:
            soma = len(nodes)
            nodes.append([cx, cy, -1, -1, col, 0.0, math.hypot(cx, cy) - 1.12])
            _grow(nodes, soma, [(rng.uniform(0, TAU), rng.uniform(0.30, 0.48), col)
                                for _ in range(4)], 2, 0.70, 0.72, rng, lim)

        dust = []
        while len(dust) < 160:
            x, y = rng.gauss(0, 1.9), rng.gauss(0, 1.5)
            if x * x + y * y > 1.6 and abs(x) < lim[0] and abs(y) < lim[1]:
                dust.append((x, y, rng.uniform(0.5, 1.3), rng.randrange(7), rng.random() * TAU))
        nebula = [(cx, cy, 1.9, col, 1.0) for cx, cy, col in centers]
        nebula += [(0.0, 0.0, 4.4, 6, 1.5), (0.0, 0.0, 2.4, 0, 1.2)]
        return {
            "particles": particles, "members": members, "centers": centers,
            "ccol": [c[2] for c in centers], "edges": edges,
            "tree": _pack_tree(nodes), "dust": dust, "stars": _stars(rng, 150),
            "nebula": nebula, "core": (0.0, 0.0), "sway": 3.0, "dscale": 3.2,
            "bend": 0.6, "fil": 16,
        }

    def _build_orb_assets(self, rng) -> None:
        """Globe mesh, orbital rings and the particle swarm for Visualization 2."""
        n = 240
        golden = math.pi * (3.0 - math.sqrt(5.0))
        sph = []
        for i in range(n):
            y = 1.0 - 2.0 * (i + 0.5) / n
            ring = math.sqrt(max(0.0, 1.0 - y * y))
            sph.append((math.cos(i * golden) * ring, y, math.sin(i * golden) * ring))
        self._neural_sphere = sph
        self._neural_sphere_edges = [
            (i, j) for i in range(n) for j in range(i + 1, n)
            if sph[i][0] * sph[j][0] + sph[i][1] * sph[j][1] + sph[i][2] * sph[j][2] > 0.965
        ]
        # Spectral band per vertex: a lat/long sweep that reads as an iridescent skin.
        self._neural_sphere_band = [
            int(((math.atan2(x, z) / TAU + 0.5) + (y + 1.0) * 0.16) * BANDS) % BANDS
            for x, y, z in sph
        ]
        bodies = []
        for _ in range(150):
            r = rng.uniform(1.30, 2.25)
            bodies.append((r, rng.uniform(-1.25, 1.25), rng.uniform(0, TAU),
                           rng.uniform(0, TAU),
                           rng.choice((-1, 1)) * rng.uniform(0.35, 0.95) / math.sqrt(r),
                           rng.choice((0.7, 0.9, 1.2, 1.6)), rng.randrange(RAMP_N)))
        # radius, inclination, node, comet speed
        rings = ((1.42, 0.55, 0.30, 0.55), (1.72, 1.12, 2.10, -0.42),
                 (1.30, -0.72, 4.00, 0.70), (2.02, 0.20, 1.20, -0.30))
        self._neural_orbit = {"bodies": bodies, "rings": rings}

    # ------------------------------------------------------------- entry
    def _paint_neural_network(self, p: QPainter, W: float, H: float,
                              cx: float, cy: float, fw: float,
                              orb_mode: bool = False):
        """Paint Visualization 1 (synapse field) or 2 (neural orb)."""
        if self._neural_failed:
            self._nrl_fallback(p, cx, cy, fw)
            return
        try:
            self._nrl_render(p, W, H, cx, cy, fw, bool(orb_mode))
        except Exception:                      # never let a paint bug kill the HUD
            self._neural_failed = True
            traceback.print_exc(file=sys.stderr)
            p.setCompositionMode(OVER)
            p.setOpacity(1.0)
            self._nrl_fallback(p, cx, cy, fw)

    def _nrl_fallback(self, p, cx, cy, fw):
        size = fw * 0.6
        p.setCompositionMode(OVER)
        p.setOpacity(0.8)
        p.drawImage(QRectF(cx - size / 2, cy - size / 2, size, size), self._neural_glows[7])
        p.setOpacity(1.0)

    def _nrl_render(self, p, W, H, cx, cy, fw, orb):
        lay = self._neural_layouts[orb]
        if orb:
            kx = ky = fw * 0.185                     # one globe radius, in pixels
            core = (cx, cy)
        else:
            kx = min(W * 0.62, H * 0.82)
            ky = min(H * 0.70, W * 0.42)
            core = (cx + kx * lay["core"][0], cy + ky * lay["core"][1])
        active = self.state in ("THINKING", "PROCESSING") or self.speaking
        v = SimpleNamespace(
            p=p, W=W, H=H, cx=cx, cy=cy, fw=fw, kx=kx, ky=ky, orb=orb, lay=lay,
            core=core, active=active, motion=1.0 if active else 0.42,
            energy=_clamp(self._neural_energy), flash=_clamp(self._neural_flash),
            amp=_clamp(self._amp_disp), phase=self._neural_phase,
        )
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        self._nrl_backdrop(v)
        p.setCompositionMode(PLUS)
        self._nrl_tree(v)
        self._nrl_filaments(v)
        pos = self._nrl_particles(v)
        self._nrl_edges(v, pos)
        if orb:
            self._nrl_globe(v)
        else:
            self._nrl_core(v)
        p.setCompositionMode(OVER)
        p.setOpacity(1.0)
        self._nrl_tags(v, pos)

    # ---------------------------------------------------------- backdrop
    def _nrl_nebula(self, v):
        """Soft coloured haze, rendered once per window size at half resolution."""
        sw, sh = max(2, int(v.W) // 2), max(2, int(v.H) // 2)
        img = QImage(sw, sh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        g = QPainter(img)
        g.setRenderHint(QPainter.RenderHint.Antialiasing)
        g.setCompositionMode(PLUS)
        g.setPen(NOPEN)
        pal = self._neural_rgb
        for x, y, size, col, strength in v.lay["nebula"]:
            px, py = (v.cx + x * v.kx) * 0.5, (v.cy + y * v.ky) * 0.5
            rad = size * max(v.kx, v.ky) * 0.5
            grad = QRadialGradient(px, py, rad)
            grad.setColorAt(0.0, _rgba(pal[col], 44 * strength))
            grad.setColorAt(0.45, _rgba(pal[col], 18 * strength))
            grad.setColorAt(1.0, _rgba(pal[col], 0))
            g.setBrush(QBrush(grad))
            g.drawEllipse(QRectF(px - rad, py - rad, rad * 2, rad * 2))
        g.end()
        return img

    def _nrl_backdrop(self, v):
        p, lay, phase = v.p, v.lay, v.phase
        key = (int(v.W), int(v.H), int(v.kx), int(v.ky))
        entry = self._neural_bg.get(v.orb)
        if entry is None or entry[0] != key:
            entry = (key, self._nrl_nebula(v))
            self._neural_bg[v.orb] = entry
        p.setCompositionMode(PLUS)
        p.setOpacity(_clamp(0.80 + 0.10 * math.sin(phase * 0.16) + 0.12 * v.energy
                            + 0.10 * v.flash))
        p.drawImage(QRectF(0, 0, v.W, v.H), entry[1])
        p.setOpacity(1.0)

        pal, sin = self._neural_rgb, math.sin
        stars = {}
        for x, y, size, off, layer, tint in lay["stars"]:
            tw = 0.5 + 0.5 * sin(phase * (0.5 + 0.25 * layer) + off)
            a = 26 + 120 * tw * (0.30 + v.energy * 0.70) * (0.6 + 0.25 * layer)
            lvl = 0 if a < 55 else (1 if a < 95 else 2)
            sx = v.cx + x * v.W * 0.51 + sin(phase * 0.07 + off) * 1.5 * layer
            sy = v.cy + y * v.H * 0.51
            r = size * (0.55 + 0.2 * lvl)
            _bucket(stars, (tint, lvl), _dots).addEllipse(QRectF(sx - r, sy - r, r * 2, r * 2))
        p.setPen(NOPEN)
        for (tint, lvl), path in stars.items():
            rgb = _mix(pal[tint], WHITE, 0.45) if tint < 7 else (190, 208, 255)
            p.setBrush(QBrush(_rgba(rgb, (70, 130, 205)[lvl])))
            p.drawPath(path)

        dust = {}
        ds, kx, ky = lay["dscale"], v.kx, v.ky
        for x, y, size, col, off in lay["dust"]:
            tw = 0.5 + 0.5 * sin(phase * 0.8 + off)
            px = v.cx + (x + sin(phase * 0.3 + off) * 0.01 * ds * v.motion) * kx
            py = v.cy + (y + math.cos(phase * 0.27 + off) * 0.01 * ds * v.motion) * ky
            r = size * (0.7 + 0.5 * tw)
            _bucket(dust, (col, 0 if tw < 0.5 else 1), _dots).addEllipse(
                QRectF(px - r, py - r, r * 2, r * 2))
        for (col, lvl), path in dust.items():
            p.setBrush(QBrush(_rgba(pal[col], (55 + 40 * v.energy, 120 + 50 * v.energy)[lvl])))
            p.drawPath(path)

    # -------------------------------------------------------------- tree
    def _nrl_tree(self, v):
        """Branching arbors. A wave of light runs outward along every branch
        (continuously, and as a shockwave whenever JARVIS starts an operation)."""
        p, T = v.p, v.lay["tree"]
        xs, ys, par, dep = T["x"], T["y"], T["par"], T["dep"]
        col, seed, dist, leaf = T["col"], T["seed"], T["dist"], T["leaf"]
        energy, flash, amp, phase, motion = v.energy, v.flash, v.amp, v.phase, v.motion
        sway, ox, oy, kx, ky = v.lay["sway"], v.cx, v.cy, v.kx, v.ky
        sin, cos, exp, hypot = math.sin, math.cos, math.exp, math.hypot
        pal = self._neural_rgb

        front = (phase * (0.075 + 0.05 * energy)) % 1.55
        shock = (1.0 - flash) * 1.45 if flash > 0.03 else None
        pos = [None] * len(xs)
        paths, beads, glows = {}, {}, []
        for i in range(len(xs)):
            s, d = seed[i], dep[i]
            amt = (0.004 + 0.007 * (d + 1)) * sway * motion
            a = phase * 0.55 + s
            px = ox + (xs[i] + sin(a) * amt) * kx
            py = oy + (ys[i] + cos(a * 0.83) * amt) * ky
            pos[i] = (px, py)
            pi = par[i]
            if pi < 0:
                continue
            qx, qy = pos[pi]
            dx, dy = px - qx, py - qy
            bow = (s - math.pi) / math.pi * 0.34 + sin(phase * 0.9 + s * 2.0) * 0.08 * motion
            mx, my = (px + qx) * 0.5 - dy * bow, (py + qy) * 0.5 + dx * bow

            dd = dist[i] - front
            lit = exp(-dd * dd * 260.0)
            if shock is not None:
                de = dist[i] - shock
                lit += 1.25 * exp(-de * de * 160.0)
            if sin(phase * 1.1 + s * 5.0) > 0.965:
                lit += 0.7
            hot = lit > 0.30
            band = 0 if d < 2 else (1 if d < 3 else 2)
            path = _bucket(paths, (col[i], band, hot))
            path.moveTo(QPointF(qx, qy))
            path.quadTo(QPointF(mx, my), QPointF(px, py))
            if hot and lit > 0.55 and (i & 1) == 0 and len(glows) < 46:
                glows.append((px, py, col[i], lit))
            if leaf[i] or d == 0:
                r = (1.9 + 0.8 * min(1.0, lit)) if hot else (1.0 if leaf[i] else 1.3)
                _bucket(beads, (col[i], hot), _dots).addEllipse(QRectF(px - r, py - r, r * 2, r * 2))

        widths = (1.9, 1.25, 0.8)
        wscale = 0.8 if v.orb else 1.0
        p.setBrush(NOBRUSH)
        for (c, band, hot), path in paths.items():
            rgb, w = pal[c], widths[band] * wscale
            if hot:
                aura_a, core_a, core_rgb = 46 + 36 * energy + 44 * flash, 185 + 50 * energy, _mix(rgb, WHITE, 0.45)
            else:
                aura_a, core_a, core_rgb = 13 + 14 * energy, 60 + 60 * energy + 40 * amp, rgb
            p.setPen(QPen(_rgba(rgb, aura_a), w * 3.6, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            p.setPen(QPen(_rgba(core_rgb, core_a), w, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
        p.setPen(NOPEN)
        for (c, hot), path in beads.items():
            p.setBrush(QBrush(_rgba(_mix(pal[c], WHITE, 0.5 if hot else 0.15), 235 if hot else 120)))
            p.drawPath(path)
        for px, py, c, lit in glows:
            size = 12 + 18 * min(1.0, lit)
            p.setOpacity(_clamp(0.30 + 0.35 * min(1.0, lit)))
            p.drawImage(QRectF(px - size / 2, py - size / 2, size, size), self._neural_glows[c])
        p.setOpacity(1.0)

    # --------------------------------------------------------- filaments
    def _nrl_filaments(self, v):
        """Long, flowing Bezier fibres with a bright signal riding each one.
        Synapse field: they sweep out from the core. Orb: they spiral around it."""
        p, lay, phase = v.p, v.lay, v.phase
        energy, flash, motion = v.energy, v.flash, v.motion
        ox, oy, kx, ky = v.cx, v.cy, v.kx, v.ky
        sin, cos = math.sin, math.cos
        ramp = self._neural_ramp
        shift = int(phase * 2.2)
        n = lay["fil"]
        p.setBrush(NOBRUSH)
        for i in range(n):
            if v.orb:
                a = TAU * i / n + sin(phase * 0.2 + i) * 0.06 * motion
                reach = 1.9 + 1.5 * (0.5 + 0.5 * sin(i * 4.71 + 0.4))
                s = QPointF(ox + cos(a) * 1.14 * kx, oy + sin(a) * 1.14 * ky)
                e = QPointF(ox + cos(a + 0.55) * reach * kx, oy + sin(a + 0.55) * reach * ky)
                tw = 0.52
            else:
                a = TAU * i / n + sin(phase * 0.22 + i) * 0.075 * motion
                ln = 0.36 + 0.40 * (0.5 + 0.5 * sin(i * 4.71 + 0.4))
                if i % 6 in (0, 1):
                    ln = 0.92 + 0.42 * (0.5 + 0.5 * sin(i * 2.37))     # far-reaching fibres
                s = QPointF(*v.core)
                e = QPointF(s.x() + cos(a) * kx * ln, s.y() + sin(a) * ky * ln)
                tw = 0.28
            dx, dy = e.x() - s.x(), e.y() - s.y()
            bend = sin(phase * 0.75 + i * 0.93) * tw * motion
            c1 = QPointF(s.x() + dx * 0.34 - dy * bend, s.y() + dy * 0.34 + dx * bend)
            c2 = QPointF(s.x() + dx * 0.70 + dy * bend, s.y() + dy * 0.70 - dx * bend)
            path = QPainterPath(s)
            path.cubicTo(c1, c2, e)
            wave = 0.5 + 0.5 * sin(phase * 1.2 - i * 0.66)
            rgb = ramp[((i % BANDS) * (RAMP_N // BANDS) + shift) % RAMP_N]
            p.setPen(QPen(_rgba(rgb, 16 + 42 * energy + 52 * flash + 34 * wave * energy),
                          0.55 + wave * (0.35 + flash * 0.55), SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            t = (phase * (0.20 + (i % 5) * 0.025) + i * 0.173) % 1.0
            u = 1.0 - t
            sx = u**3 * s.x() + 3 * u * u * t * c1.x() + 3 * u * t * t * c2.x() + t**3 * e.x()
            sy = u**3 * s.y() + 3 * u * u * t * c1.y() + 3 * u * t * t * c2.y() + t**3 * e.y()
            size = (15 + energy * 10 + flash * 12) if i % 3 == 0 else 8
            p.setOpacity(_clamp(0.30 + energy * 0.25 + flash * 0.20))
            p.drawImage(QRectF(sx - size / 2, sy - size / 2, size, size),
                        self._neural_band_glows[(i % BANDS + shift // 4) % BANDS])
        p.setOpacity(1.0)

    # --------------------------------------------------------- particles
    def _nrl_particles(self, v):
        """Neuron clouds: drifting elongated bodies with bloom. Returns the
        screen positions so the synapse edges can attach to them."""
        p, lay = v.p, v.lay
        phase, energy, flash, motion = v.phase, v.energy, v.flash, v.motion
        sin, cos = math.sin, math.cos
        ox, oy, kx, ky, ds = v.cx, v.cy, v.kx, v.ky, lay["dscale"]
        pal, glows = self._neural_rgb, self._neural_glows
        fire = (self._neural_call_count * 3 + (1 if self.speaking else 0)) % 7
        waves = [0.5 + 0.5 * sin(phase * (0.78 + r * 0.075) + r * 1.91) for r in range(7)]
        pos, dots, trails, hubs = [], {}, {}, []
        p.setBrush(NOBRUSH)
        for index, (x, y, radius, col, off, drift, cl) in enumerate(lay["particles"]):
            drive = 0.16 + 0.44 * energy + 0.22 * waves[cl] + flash * (0.42 if cl == fire else 0.09)
            amt = drift * ds * motion
            px = ox + (x + sin(phase * (0.65 + drift * 18 + cl * 0.035) + off) * amt) * kx
            py = oy + (y + cos(phase * (0.58 + cl * 0.027) + off) * amt) * ky
            pos.append(QPointF(px, py))
            pulse = 0.5 + 0.5 * sin(phase * (0.9 + drift * 12 + cl * 0.06) + off + cl * 0.93)
            bright = min(1.0, 0.18 + drive * 0.55 + pulse * 0.20 + flash * 0.22)
            bucket = min(2, int(bright * 3))
            r = radius * (0.72 + 0.70 * pulse + energy * 0.36)
            _bucket(dots, (col, bucket), _dots).addEllipse(QRectF(px - r, py - r, r * 2, r * 2))
            ang = off + phase * (0.16 + 0.10 * motion) + cl * 0.71
            ux, uy = cos(ang), sin(ang)
            ln = (2.6 + radius * 3.6) * (0.70 + pulse * 0.62 + drive * 0.48)
            tr = _bucket(trails, (col, bucket))
            tr.moveTo(QPointF(px - ux * ln * 0.82, py - uy * ln * 0.82))
            tr.lineTo(QPointF(px + ux * ln * 0.34, py + uy * ln * 0.34))
            if radius > 2.0:
                hubs.append((px, py, col, pulse))
            if index % 16 == 0 and (pulse > 0.54 or v.active):
                size = 22 + 18 * pulse + 12 * energy + 8 * flash
                p.setOpacity(_clamp(0.22 + pulse * 0.17 + energy * 0.17 + flash * 0.20))
                p.drawImage(QRectF(px - size / 2, py - size / 2, size, size), glows[col])
        p.setOpacity(1.0)
        for (col, bucket), path in trails.items():
            rgb = pal[col]
            p.setPen(QPen(_rgba(rgb, (34, 52, 78)[bucket] + 26 * energy + 24 * flash),
                          (3.0, 3.8, 4.8)[bucket], SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            p.setPen(QPen(_rgba(_mix(rgb, WHITE, 0.25), min(255, (110, 175, 238)[bucket] + 12 * energy)),
                          (0.7, 0.95, 1.3)[bucket], SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
        p.setPen(NOPEN)
        for (col, bucket), path in dots.items():
            p.setBrush(QBrush(_rgba(_mix(pal[col], WHITE, 0.2), (68, 128, 205)[bucket])))
            p.drawPath(path)
        for px, py, col, pulse in hubs:        # hub neurons get a four-point flare
            size = 26 + 22 * pulse + 14 * energy + 14 * flash
            p.setOpacity(_clamp(0.40 + 0.35 * pulse + 0.2 * flash))
            p.drawImage(QRectF(px - size / 2, py - size / 2, size, size), self._neural_flares[col])
        p.setOpacity(1.0)
        return pos

    # ------------------------------------------------------------- edges
    def _nrl_edges(self, v, pos):
        """Synapse links. Local ones are batched by region; long inter-region
        axons are drawn singly so each can carry a two-colour gradient."""
        p, lay = v.p, v.lay
        phase, energy, flash = v.phase, v.energy, v.flash
        sin = math.sin
        pal, ccol = self._neural_rgb, lay["ccol"]
        local, sparks, cross = {}, {}, []
        bscale = lay["bend"]
        for ei, (a, b, c, off, dr, dst) in enumerate(lay["edges"]):
            st, en = pos[a], pos[b]
            pulse = 0.5 + 0.5 * sin(phase * dr * (1.1 + energy + c * 0.08) + off + c * 1.27)
            bend = sin(off + phase * (0.24 + c * 0.025)) * (8 + 24 * energy) * bscale
            if dst != c:
                # long axons arc in proportion to their length, so they read as
                # organic fibres rather than ruled lines
                dx, dy = en.x() - st.x(), en.y() - st.y()
                arc = (0.20 + 0.08 * sin(off + phase * 0.3)) * (1.0 if off < math.pi else -1.0)
                mid = QPointF((st.x() + en.x()) * 0.5 - dy * arc, (st.y() + en.y()) * 0.5 + dx * arc)
                cross.append((st, mid, en, c, dst, pulse, off, dr))
                continue
            mid = QPointF((st.x() + en.x()) * 0.5 + bend, (st.y() + en.y()) * 0.5 - bend * 0.4)
            bucket = 0 if pulse < 0.48 else (1 if pulse < 0.82 else 2)
            path = _bucket(local, (ccol[c], bucket))
            path.moveTo(st)
            path.quadTo(mid, en)
            if ei % 7 == 0 and pulse > 0.68:
                t = (phase * dr * 0.34 + off / TAU) % 1.0
                u = 1.0 - t
                sx = u * u * st.x() + 2 * u * t * mid.x() + t * t * en.x()
                sy = u * u * st.y() + 2 * u * t * mid.y() + t * t * en.y()
                _bucket(sparks, ccol[c], _dots).addEllipse(QRectF(sx - 1.0, sy - 1.0, 2.0, 2.0))
        p.setBrush(NOBRUSH)
        for (col, bucket), path in local.items():
            rgb = pal[col]
            p.setPen(QPen(_rgba(rgb, (24, 40, 62)[bucket] + 18 * energy + 20 * flash),
                          (3.0, 4.0, 5.0)[bucket], SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            p.setPen(QPen(_rgba(rgb, (72, 138, 210)[bucket] + 16 * energy + 20 * flash),
                          (0.55, 0.85, 1.2)[bucket], SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
        for st, mid, en, c, dst, pulse, off, dr in cross:
            ca, cb = pal[ccol[c]], pal[ccol[dst]]
            alpha = 36 + 110 * pulse + 30 * energy + 40 * flash
            grad = QLinearGradient(st, en)
            grad.setColorAt(0.0, _rgba(ca, alpha))
            grad.setColorAt(1.0, _rgba(cb, alpha))
            path = QPainterPath(st)
            path.quadTo(mid, en)
            p.setPen(QPen(QBrush(grad), 0.7 + 1.1 * pulse, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            if pulse > 0.55:                   # a bead of light crosses the axon
                t = (phase * dr * 0.22 + off / TAU) % 1.0
                u = 1.0 - t
                sx = u * u * st.x() + 2 * u * t * mid.x() + t * t * en.x()
                sy = u * u * st.y() + 2 * u * t * mid.y() + t * t * en.y()
                size = 10 + 12 * pulse
                p.setOpacity(_clamp(0.35 + 0.4 * pulse))
                p.drawImage(QRectF(sx - size / 2, sy - size / 2, size, size),
                            self._neural_glows[ccol[dst] if t > 0.5 else ccol[c]])
        p.setOpacity(1.0)
        p.setPen(NOPEN)
        for col, path in sparks.items():
            p.setBrush(QBrush(_rgba(_mix(pal[col], WHITE, 0.5), 235)))
            p.drawPath(path)

    # -------------------------------------------------------------- core
    def _nrl_core(self, v):
        """The white-hot nucleus of the synapse field."""
        p, fw = v.p, v.fw
        energy, flash, amp, phase = v.energy, v.flash, v.amp, v.phase
        x, y = v.core
        sin, cos = math.sin, math.cos
        ramp, glows = self._neural_ramp, self._neural_glows
        shift = int(phase * 2.0)
        swell = 1.0 + 0.10 * energy + 0.12 * amp + 0.08 * flash + 0.03 * sin(phase * 1.7)
        lift = 0.80 + 0.35 * energy + 0.25 * amp + 0.20 * flash
        for idx, scale, op in ((6, 0.74, 0.34), (0, 0.50, 0.42), (1, 0.34, 0.46),
                               (3, 0.22, 0.40), (7, 0.15, 0.80), (7, 0.07, 1.0)):
            size = fw * scale * swell
            p.setOpacity(_clamp(op * lift))
            p.drawImage(QRectF(x - size / 2, y - size / 2, size, size), glows[idx])

        fl = fw * (0.34 + 0.08 * energy + 0.10 * amp + 0.10 * flash)
        p.save()
        p.translate(x, y)
        p.rotate(math.degrees(phase * 0.10))
        p.setOpacity(_clamp(0.55 + 0.30 * energy + 0.20 * flash))
        p.drawImage(QRectF(-fl / 2, -fl / 2, fl, fl), self._neural_flares[7])
        p.rotate(33.0)
        p.setOpacity(_clamp(0.30 + 0.25 * energy))
        p.drawImage(QRectF(-fl * 0.35, -fl * 0.35, fl * 0.7, fl * 0.7), self._neural_flares[3])
        p.restore()

        p.setBrush(NOBRUSH)
        for i in range(14):                    # slow, counter-rotating spokes
            ang = TAU * i / 14 + phase * 0.06 * (1 if i % 2 else -1)
            ln = fw * (0.10 + 0.07 * (0.5 + 0.5 * sin(phase * 1.3 + i * 2.1)) + 0.10 * energy + 0.05 * flash)
            r0 = fw * 0.026
            p.setPen(QPen(_rgba(ramp[(i * 3 + shift) % RAMP_N], 60 + 70 * energy + 60 * flash),
                          1.0, SOLID, ROUND, ROUNDJ))
            p.drawLine(QPointF(x + cos(ang) * r0, y + sin(ang) * r0),
                       QPointF(x + cos(ang) * (r0 + ln), y + sin(ang) * (r0 + ln)))

        rings = [((phase * 0.10 + k * 0.5) % 1.0, 46 + 70 * energy + 60 * amp) for k in range(2)]
        if flash > 0.03:
            rings.append((1.0 - flash, 210 * flash + 40))
        for t, strength in rings:              # expanding shock rings
            rr = fw * (0.05 + 0.46 * t)
            p.setPen(QPen(_rgba(ramp[(int(t * RAMP_N) + shift) % RAMP_N], (1 - t) ** 2 * strength),
                          1.0 + (1 - t) * 1.6))
            p.drawEllipse(QRectF(x - rr, y - rr, rr * 2, rr * 2))

        for k in range(5):                     # electrons orbiting the nucleus
            a = phase * (0.9 + k * 0.37) + k * 1.26
            rx = fw * (0.050 + 0.012 * k)
            ry, tilt = rx * 0.46, k * 0.7 + phase * 0.05
            ex, ey = rx * cos(a), ry * sin(a)
            ex, ey = x + ex * cos(tilt) - ey * sin(tilt), y + ex * sin(tilt) + ey * cos(tilt)
            size = 11 + 3 * k
            p.setOpacity(0.85)
            p.drawImage(QRectF(ex - size / 2, ey - size / 2, size, size),
                        self._neural_band_glows[(k * 2 + shift // 4) % BANDS])
        p.setOpacity(1.0)
        p.setPen(NOPEN)
        p.setBrush(QBrush(QColor(255, 255, 255, 235)))
        r = fw * (0.013 + energy * 0.005 + amp * 0.007)
        p.drawEllipse(QRectF(x - r, y - r, r * 2, r * 2))

    # ------------------------------------------------------------- globe
    def _nrl_orbit_geometry(self, v):
        """Project the orbital rings and particle swarm once per frame, split
        into a back half (drawn behind the globe) and a front half."""
        unit, cx, cy, phase = v.kx, v.cx, v.cy, v.phase
        sin, cos = math.sin, math.cos
        cp, sp = cos(0.24), sin(0.24)

        def place(r, ci, si, cn, sn, th):
            x1, z1 = cos(th) * r, sin(th) * r
            y2, z2 = -z1 * si, z1 * ci
            x3, z3 = x1 * cn + z2 * sn, -x1 * sn + z2 * cn
            return cx + x3 * unit, cy + (y2 * cp - z3 * sp) * unit, y2 * sp + z3 * cp

        rings = []
        for k, (r, incl, node, speed) in enumerate(self._neural_orbit["rings"]):
            ci, si = cos(incl), sin(incl)
            nd = node + phase * 0.04 * (1 if k % 2 else -1)
            cn, sn = cos(nd), sin(nd)
            back, front = QPainterPath(), QPainterPath()
            side, prev = None, place(r, ci, si, cn, sn, 0.0)
            for s in range(1, 65):
                cur = place(r, ci, si, cn, sn, TAU * s / 64)
                sd = (prev[2] + cur[2]) > 0
                path = front if sd else back
                if sd != side:
                    path.moveTo(QPointF(prev[0], prev[1]))
                    side = sd
                path.lineTo(QPointF(cur[0], cur[1]))
                prev = cur
            rings.append((k, back, front, place(r, ci, si, cn, sn, phase * speed + k * 1.7)))

        dots, trails, bloom = {}, {}, []
        for r, incl, node, th0, speed, size, ri in self._neural_orbit["bodies"]:
            ci, si = cos(incl), sin(incl)
            nd = node + phase * 0.03
            cn, sn = cos(nd), sin(nd)
            th = th0 + phase * speed
            x, y, z = place(r, ci, si, cn, sn, th)
            fr, band = z > 0, ri // (RAMP_N // BANDS)
            rr = size * (1.0 if fr else 0.7) * (1.0 + 0.25 * v.energy)
            _bucket(dots, (fr, band), _dots).addEllipse(QRectF(x - rr, y - rr, rr * 2, rr * 2))
            if size > 1.0:
                x0, y0, _ = place(r, ci, si, cn, sn, th - 0.16 * (1 if speed > 0 else -1))
                tr = _bucket(trails, (fr, band))
                tr.moveTo(QPointF(x0, y0))
                tr.lineTo(QPointF(x, y))
            if fr and size >= 1.6 and len(bloom) < 24:
                bloom.append((x, y, band))
        return {"rings": rings, "dots": dots, "trails": trails, "bloom": bloom,
                "shift": int(phase * 2.4) % RAMP_N}

    def _nrl_draw_orbit(self, v, geo, front):
        p, ramp = v.p, self._neural_ramp
        shift, energy, amp = geo["shift"], v.energy, v.amp
        p.setCompositionMode(PLUS)
        p.setBrush(NOBRUSH)
        for k, back, frontp, comet in geo["rings"]:
            rgb = ramp[(k * 12 + shift) % RAMP_N]
            a = (150 if front else 46) + 40 * energy + 40 * amp
            path = frontp if front else back
            p.setPen(QPen(_rgba(rgb, a * 0.28), 3.2, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            p.setPen(QPen(_rgba(_mix(rgb, WHITE, 0.3), a), 0.9, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            if (comet[2] > 0) == front:        # a comet rides each ring
                size = 20 + 8 * energy
                p.setOpacity(0.9 if front else 0.4)
                p.drawImage(QRectF(comet[0] - size / 2, comet[1] - size / 2, size, size),
                            self._neural_band_glows[(k * 3 + shift // 4) % BANDS])
                p.setOpacity(1.0)
        for (fr, band), path in geo["trails"].items():
            if fr == front:
                rgb = ramp[(band * 4 + shift) % RAMP_N]
                p.setPen(QPen(_rgba(rgb, 170 if front else 60), 0.9, SOLID, ROUND, ROUNDJ))
                p.drawPath(path)
        p.setPen(NOPEN)
        for (fr, band), path in geo["dots"].items():
            if fr == front:
                p.setBrush(QBrush(_rgba(_mix(ramp[(band * 4 + shift) % RAMP_N], WHITE, 0.3),
                                        215 if front else 90)))
                p.drawPath(path)
        if front:
            for x, y, band in geo["bloom"]:
                p.setOpacity(0.45)
                p.drawImage(QRectF(x - 7, y - 7, 14, 14), self._neural_band_glows[(band + shift // 4) % BANDS])
            p.setOpacity(1.0)

    def _nrl_globe(self, v):
        """The luminous glass globe: iridescent firing mesh, a glowing nucleus
        seen through the glass, a scanning ring, rim light and orbiting swarm."""
        p = v.p
        energy, flash, amp, phase = v.energy, v.flash, v.amp, v.phase
        cx, cy = v.cx, v.cy
        sin, cos, exp, acos = math.sin, math.cos, math.exp, math.acos
        ramp, glows, bglows = self._neural_ramp, self._neural_glows, self._neural_band_glows
        shift = int(phase * 2.4) % RAMP_N
        pulse = 0.5 + 0.5 * sin(phase * (1.9 if v.active else 0.8))
        R = v.kx * (0.97 + 0.045 * energy + 0.05 * amp + 0.02 * pulse)
        boost = 0.8 + 0.5 * energy + 0.4 * amp + 0.2 * flash

        # 1. atmosphere: wide additive halos
        p.setCompositionMode(PLUS)
        for idx, scale, op in ((6, 5.6, 0.50), (0, 3.7, 0.50), (3, 2.7, 0.20)):
            size = R * scale * (1.0 + 0.04 * amp)
            p.setOpacity(_clamp(op * boost))
            p.drawImage(QRectF(cx - size / 2, cy - size / 2, size, size), glows[idx])
        p.setOpacity(1.0)

        geo = self._nrl_orbit_geometry(v)
        self._nrl_draw_orbit(v, geo, front=False)

        # 2. glass body: dark indigo volume with a bright Fresnel rim
        p.setCompositionMode(OVER)
        body = QRadialGradient(cx, cy, R * 1.02)
        body.setColorAt(0.00, QColor(30, 16, 84, 40))
        body.setColorAt(0.60, QColor(36, 26, 120, 52))
        body.setColorAt(0.84, QColor(88, 82, 255, 78))
        body.setColorAt(0.955, QColor(150, 215, 255, min(255, 130 + int(80 * energy + 60 * amp))))
        body.setColorAt(1.00, QColor(190, 235, 255, 0))
        p.setPen(NOPEN)
        p.setBrush(QBrush(body))
        p.drawEllipse(QRectF(cx - R * 1.02, cy - R * 1.02, R * 2.04, R * 2.04))

        # 3. nucleus and swirling inner colour, seen through the glass
        p.setCompositionMode(PLUS)
        size = R * (1.3 + 0.25 * energy + 0.35 * amp + 0.2 * flash)
        p.setOpacity(_clamp(0.40 + 0.18 * energy + 0.20 * amp + 0.15 * flash))
        p.drawImage(QRectF(cx - size / 2, cy - size / 2, size, size), glows[7])
        for k in range(3):
            a = phase * (0.55 + 0.2 * k) + k * 2.094
            ix, iy = cx + cos(a) * R * 0.34, cy + sin(a * 1.3) * R * 0.28
            size = R * 1.15
            p.setOpacity(0.46 + 0.1 * energy)
            p.drawImage(QRectF(ix - size / 2, iy - size / 2, size, size),
                        bglows[((shift + k * 16) % RAMP_N) // (RAMP_N // BANDS)])
        p.setOpacity(1.0)

        # 4. rotate and project the mesh
        yaw = phase * (0.12 + 0.23 * energy + 0.11 * amp)
        pitch = 0.24 + sin(phase * 0.23) * 0.10
        ca, sa, cp, sp = cos(yaw), sin(yaw), cos(pitch), sin(pitch)

        def project(x, y, z):
            xr, zr = x * ca + z * sa, -x * sa + z * ca
            yr, z2 = y * cp - zr * sp, y * sp + zr * cp
            f = 1.0 / max(0.78, 1.0 - z2 * 0.12)
            return cx + xr * R * f, cy + yr * R * f, z2

        sph, bands = self._neural_sphere, self._neural_sphere_band
        n = len(sph)
        px, py, pz = [0.0] * n, [0.0] * n, [0.0] * n
        for i, (x, y, z) in enumerate(sph):
            px[i], py[i], pz[i] = project(x, y, z)

        # firing waves: one rolls across the surface continuously, and a faster
        # one is launched from a new spot whenever JARVIS starts an operation
        k = self._neural_call_count
        lon0, lat0 = phase * 0.19 + k * 2.399, sin(phase * 0.11 + k * 1.7) * 0.95
        s0 = (cos(lat0) * sin(lon0), sin(lat0), cos(lat0) * cos(lon0))
        wave_at = ((phase * 0.36) % 1.3) * math.pi
        lonf, latf = k * 5.113 + 0.7, sin(k * 3.1) * 0.8
        sf = (cos(latf) * sin(lonf), sin(latf), cos(latf) * cos(lonf))
        flash_at = (1.0 - flash) * 1.2 * math.pi if flash > 0.03 else None
        gain = 0.7 + 0.5 * energy
        lit = [0.0] * n
        for i, (x, y, z) in enumerate(sph):
            e = acos(_clamp(s0[0] * x + s0[1] * y + s0[2] * z, -1.0, 1.0)) - wave_at
            val = exp(-e * e / 0.18)
            if flash_at is not None:
                e2 = acos(_clamp(sf[0] * x + sf[1] * y + sf[2] * z, -1.0, 1.0)) - flash_at
                val += 1.2 * exp(-e2 * e2 / 0.26)
            tw = 0.5 + 0.5 * sin(phase * 1.4 + i * 1.73)
            lit[i] = min(1.4, val * gain + tw * tw * tw * 0.35 * (0.3 + energy + amp))

        mesh, hot = {}, {}
        for a, b in self._neural_sphere_edges:
            zm = (pz[a] + pz[b]) * 0.5
            lvl = 0 if zm < -0.18 else (1 if zm < 0.35 else 2)
            pa, pb = QPointF(px[a], py[a]), QPointF(px[b], py[b])
            path = _bucket(mesh, (bands[a], lvl))
            path.moveTo(pa)
            path.lineTo(pb)
            if (lit[a] + lit[b]) * 0.5 > 0.34 and zm > -0.30:
                hp = _bucket(hot, bands[a])
                hp.moveTo(pa)
                hp.lineTo(pb)
        p.setBrush(NOBRUSH)
        for (bd, lvl), path in mesh.items():
            rgb = ramp[(bd * (RAMP_N // BANDS) + shift) % RAMP_N]
            p.setPen(QPen(_rgba(rgb, (22, 58, 108)[lvl] + 18 * energy + 28 * amp + 18 * flash),
                          (0.55, 0.8, 1.05)[lvl], SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
        for bd, path in hot.items():
            rgb = ramp[(bd * (RAMP_N // BANDS) + shift) % RAMP_N]
            p.setPen(QPen(_rgba(rgb, 34 + 22 * energy + 24 * flash), 3.4, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)
            p.setPen(QPen(_rgba(_mix(rgb, WHITE, 0.15), 175), 1.15, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)

        # 5. meridians, parallels and the scanning ring
        def trace(points, rgb, alpha, width, aura=0.0):
            path = QPainterPath()
            for j, (x, y, z) in enumerate(points):
                sx, sy, _ = project(x, y, z)
                if j:
                    path.lineTo(QPointF(sx, sy))
                else:
                    path.moveTo(QPointF(sx, sy))
            if aura:
                p.setPen(QPen(_rgba(rgb, alpha * 0.3), aura, SOLID, ROUND, ROUNDJ))
                p.drawPath(path)
            p.setPen(QPen(_rgba(rgb, alpha), width, SOLID, ROUND, ROUNDJ))
            p.drawPath(path)

        for j, lon in enumerate((-0.78, -0.30, 0.22, 0.76)):
            pts = [(sin(lon) * cos(-math.pi / 2 + math.pi * s / 36), sin(-math.pi / 2 + math.pi * s / 36),
                    cos(lon) * cos(-math.pi / 2 + math.pi * s / 36)) for s in range(37)]
            trace(pts, ramp[(shift + j * 12) % RAMP_N], 95 + 72 * energy + 50 * amp, 0.9 + amp * 0.45, aura=3.2)
        for j, lat in enumerate((-0.55, 0.0, 0.55)):
            pts = [(cos(lat) * cos(TAU * s / 54), sin(lat), cos(lat) * sin(TAU * s / 54)) for s in range(55)]
            trace(pts, ramp[(shift + 8 + j * 14) % RAMP_N], 75 + 70 * energy + 46 * amp, 0.8, aura=2.6)
        scan = sin(phase * 0.45) * 1.15
        pts = [(cos(scan) * cos(TAU * s / 72), sin(scan), cos(scan) * sin(TAU * s / 72)) for s in range(73)]
        trace(pts, (190, 240, 255), 110 + 90 * energy + 50 * flash, 1.1, aura=4.0)

        # 6. surface neurons
        nodes, flares, bloom = {}, [], []
        for i in range(n):
            front = pz[i] > 0.02
            tw = 0.5 + 0.5 * sin(phase * 1.4 + i * 1.73)
            nr = 0.7 + (0.6 if front else 0.2) + 0.55 * tw * (0.3 + energy + amp) \
                + (1.6 if front else 0.6) * min(1.0, lit[i])
            _bucket(nodes, (bands[i], front), _dots).addEllipse(
                QRectF(px[i] - nr, py[i] - nr, nr * 2, nr * 2))
            if front and lit[i] > 0.45 and len(bloom) < 40:
                bloom.append((px[i], py[i], bands[i], lit[i]))
            if front and lit[i] > 0.85 and len(flares) < 6:
                flares.append((px[i], py[i], bands[i]))
        p.setPen(NOPEN)
        for (bd, front), path in nodes.items():
            rgb = ramp[(bd * (RAMP_N // BANDS) + shift) % RAMP_N]
            p.setBrush(QBrush(_rgba(_mix(rgb, WHITE, 0.25 if front else 0.0),
                                    (95, 200)[front] + 24 * energy + 32 * amp)))
            p.drawPath(path)
        for x, y, bd, val in bloom:
            size = 10 + 22 * min(1.0, val)
            p.setOpacity(_clamp(0.25 + 0.30 * min(1.0, val) + 0.15 * amp))
            p.drawImage(QRectF(x - size / 2, y - size / 2, size, size), bglows[(bd + shift // 4) % BANDS])
        for x, y, bd in flares:
            size = 34 + 16 * pulse
            p.setOpacity(0.75)
            p.drawImage(QRectF(x - size / 2, y - size / 2, size, size), self._neural_flares[bd % 7])
        p.setOpacity(1.0)

        # 7. specular highlight and a cool rim light
        p.setOpacity(0.38)
        size = R * 0.75
        p.drawImage(QRectF(cx - R * 0.40 - size / 2, cy - R * 0.44 - size / 2, size, size), glows[7])
        p.setOpacity(0.20)
        size = R * 1.1
        p.drawImage(QRectF(cx + R * 0.52 - size / 2, cy + R * 0.50 - size / 2, size, size), glows[3])
        p.setOpacity(1.0)

        self._nrl_draw_orbit(v, geo, front=True)

    # -------------------------------------------------------------- tags
    def _nrl_tags(self, v, pos):
        p, lay, W, H, fw = v.p, v.lay, v.W, v.H, v.fw
        flow = int(v.energy * 100)
        tag_w = min(150.0, max(98.0, fw * 0.34))
        tag_h = max(25.0, fw * 0.071)
        title_font = QFont("Consolas", max(6, int(fw * 0.016)), QFont.Weight.Bold)
        detail_font = QFont("Consolas", max(5, int(fw * 0.012)))
        push = 1.28 if v.orb else 1.0
        for title, col in REGIONS:
            ci = lay["ccol"].index(col)
            cxn, cyn, _ = lay["centers"][ci]
            x = _clamp(v.cx + cxn * push * v.kx - tag_w * 0.5, 4.0, W - tag_w - 4.0)
            y = _clamp(v.cy + cyn * push * v.ky - tag_h * 0.5, 18.0, H - tag_h - 34.0)
            accent = self._neural_palette[col]
            ar, ag, ab = accent.red(), accent.green(), accent.blue()
            if v.orb:                          # leader line back to the region
                p.setPen(QPen(QColor(ar, ag, ab, 78), 0.8))
                p.drawLine(QPointF(x + tag_w * 0.5, y + tag_h * 0.5),
                           QPointF(v.cx + cxn * v.kx, v.cy + cyn * v.ky))
            p.setPen(QPen(QColor(ar, ag, ab, 142), 0.8))
            p.setBrush(QBrush(QColor(8, 11, 20, 218)))
            p.drawRoundedRect(QRectF(x, y, tag_w, tag_h), 2, 2)
            p.setPen(NOPEN)
            p.setBrush(QBrush(QColor(ar, ag, ab, 225)))
            p.drawRect(QRectF(x, y + 2, 2.0, tag_h - 4))
            p.setFont(title_font)
            p.setPen(QPen(QColor(ar, ag, ab, 240), 1))
            p.drawText(QRectF(x + 6, y + 2, tag_w - 12, tag_h * 0.47),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
            p.setFont(detail_font)
            p.setPen(QPen(QColor(214, 216, 229, 190), 1))
            p.drawText(QRectF(x + 6, y + tag_h * 0.48, tag_w - 12, tag_h * 0.40),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{len(lay['members'][ci])} nodes  ·  signal {flow:02d}%")

        # Header: real state and an operation counter, nothing fabricated.
        state_label = "SPEAKING" if self.speaking else (
            "THINKING" if v.active else ("LISTENING" if self.state == "LISTENING" else "STANDBY"))
        p.setFont(QFont("Consolas", max(6, int(fw * 0.015)), QFont.Weight.Bold))
        p.setPen(QPen(QColor(214, 221, 242, 190), 1))
        name = "NEURAL ORB" if v.orb else "SYNAPSE FIELD"
        p.drawText(QRectF(20, 12, W - 40, 16), Qt.AlignmentFlag.AlignLeft, f"{name}  /  {state_label}")
        p.drawText(QRectF(20, 12, W - 40, 16), Qt.AlignmentFlag.AlignRight,
                   f"ACTIVITY {flow:02d}%  ·  CALL {self._neural_call_count:04d}")
