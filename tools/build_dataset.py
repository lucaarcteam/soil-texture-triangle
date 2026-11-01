#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Luca Bezzi
"""Generate the geometry of the 12 USDA classes from data/rules.json.

The Soil Survey Manual rules are conjunctions of linear inequalities in
(sand, silt, clay). Since sand+silt+clay=100, each inequality is a half-plane
in (sand, silt): a class polygon is the intersection of the triangle with those
half-planes. Boundaries are therefore exact, not drawn.

Classes with multiple clauses (silt loam, sandy loam) are merged with shapely
to avoid spurious internal edges.

Usage:
    python3 tools/build_dataset.py            # validate and write data/
    python3 tools/build_dataset.py --check    # validation only

Dependencies: shapely (build only, not needed by the app).
"""

import json
import math
import os
import sys

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely import set_precision
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

EPS = 1e-9

# Triangle in composition space (sand, silt); clay = 100 - sand - silt.
TRI = [(100.0, 0.0), (0.0, 100.0), (0.0, 0.0)]


# ---------------------------------------------------------------- geometry
def constraint_halfplane(k):
    """a*sand + b*silt + c*clay OP d  ->  A*sand + B*silt OP D, form f<=0."""
    a, b, c, d = k["a"], k["b"], k["c"], k["d"]
    A, B, D = a - c, b - c, d - 100.0 * c
    op = k["op"]
    if op in ("<=", "<"):
        sign = 1.0
    elif op in (">=", ">"):
        sign = -1.0
    else:
        raise ValueError("unsupported operator: %r" % op)
    return sign * A, sign * B, sign * D


def clip(poly, hp):
    """Sutherland-Hodgman: keep the part with f <= 0."""
    A, B, D = hp
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp = A * p[0] + B * p[1] - D
        fq = A * q[0] + B * q[1] - D
        pin, qin = fp <= EPS, fq <= EPS
        if pin:
            out.append(p)
        if pin != qin:
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def dedupe(poly, tol=1e-7):
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= tol and abs(out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return out


def shoelace(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def composition_to_xy(sand, silt, clay, frame):
    ax, ay = frame["apex"]
    sx, sy = frame["silt"]
    dx, dy = frame["sand"]
    total = sand + silt + clay
    x = (clay * ax + silt * sx + sand * dx) / total
    y = (clay * ay + silt * sy + sand * dy) / total
    return x, y


def xy_to_composition(x, y, frame):
    ax, ay = frame["apex"]
    sx, sy = frame["silt"]
    dx, dy = frame["sand"]
    m11, m12 = dx - ax, sx - ax
    m21, m22 = dy - ay, sy - ay
    b1, b2 = 100.0 * (x - ax), 100.0 * (y - ay)
    det = m11 * m22 - m12 * m21
    sand = (b1 * m22 - m12 * b2) / det
    silt = (m11 * b2 - b1 * m21) / det
    return sand, silt, 100.0 - sand - silt


# ------------------------------------------------------------------- rules
def satisfies(cls, sand, silt, clay, strict=True):
    for clause in cls["clauses"]:
        ok = True
        for k in clause:
            v = k["a"] * sand + k["b"] * silt + k["c"] * clay
            op, d = k["op"], k["d"]
            if op == "<=":
                ok = v <= d + (0 if strict else 1e-9)
            elif op == "<":
                ok = v < d - (1e-9 if strict else 0)
            elif op == ">=":
                ok = v >= d - (0 if strict else 1e-9)
            elif op == ">":
                ok = v > d + (1e-9 if strict else 0)
            if not ok:
                break
        if ok:
            return True
    return False


def classify_rules(classes, sand, silt, clay):
    for cls in classes:
        if satisfies(cls, sand, silt, clay):
            return cls["slug"]
    return None


def distance_to_boundaries(classes, sand, silt, clay):
    """Minimum distance (in percentage points) to the nearest boundary."""
    best = 1e9
    for cls in classes:
        for clause in cls["clauses"]:
            for k in clause:
                v = k["a"] * sand + k["b"] * silt + k["c"] * clay
                # |a*sand+b*silt+c*clay-d| / ||(a,b,c)|| with clay=100-sand-silt
                norm = math.sqrt((k["a"] - k["c"]) ** 2 + (k["b"] - k["c"]) ** 2)
                if norm < 1e-12:
                    continue
                best = min(best, abs(v - k["d"]) / norm)
    return best


# ----------------------------------------------------------------- polygons
def build(rules):
    frame = rules["frame"]
    out = []
    for cls in rules["classes"]:
        parts = []
        for clause in cls["clauses"]:
            poly = list(TRI)
            for k in clause:
                poly = dedupe(clip(poly, constraint_halfplane(k)))
                if len(poly) < 3:
                    break
            if len(poly) >= 3 and shoelace(poly) > 1e-9:
                xy = [composition_to_xy(s, si, 100.0 - s - si, frame) for s, si in poly]
                parts.append(set_precision(Polygon(xy).buffer(0), 1e-6))
        if not parts:
            out.append((cls, None))
            continue
        geom = unary_union(parts).simplify(0.01, preserve_topology=True)
        out.append((cls, geom))
    return out


def largest_polygon(geom):
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def label_point(geom):
    """Interior point as far as possible from the edges (coarse grid)."""
    g = largest_polygon(geom)
    minx, miny, maxx, maxy = g.bounds
    best, bestd = None, -1.0
    step = max(0.5, (maxx - minx) / 120.0)
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            p = Point(x, y)
            if g.covers(p):
                d = g.boundary.distance(p)
                if d > bestd:
                    bestd, best = d, (x, y)
            x += step
        y += step
    if best is None:
        rp = g.representative_point()
        best = (rp.x, rp.y)
    return best


# ------------------------------------------------------------- validation
def validate(rules, built):
    frame = rules["frame"]
    triangle = Polygon([composition_to_xy(s, si, 100 - s - si, frame) for s, si in TRI])
    tri_area = triangle.area

    print("=" * 70)
    print("Generated classes (area in %% of the triangle)")
    print("=" * 70)
    total = 0.0
    for cls, geom in built:
        if geom is None:
            print("  %-18s MISSING" % cls["slug"])
            continue
        total += geom.area
        n = len(geom.geoms) if isinstance(geom, MultiPolygon) else 1
        print("  %-18s %6.2f%%   parts=%d  vertices=%d"
              % (cls["slug"], 100 * geom.area / tri_area, n,
                 sum(len(g.exterior.coords) for g in (geom.geoms if isinstance(geom, MultiPolygon) else [geom]))))
    print("\n  area sum     = %.4f%%" % (100 * total / tri_area))
    print("  triangle     = 100.0000%%")
    print("  imbalance    = %+.6f%%" % (100 * (total - tri_area) / tri_area))

    geoms = [g for _, g in built if g is not None]

    # overlaps
    print("\n" + "-" * 70)
    print("Overlaps between classes")
    print("-" * 70)
    worst = 0.0
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            a = geoms[i].intersection(geoms[j]).area
            if a > 1e-6:
                print("  %-18s x %-18s %8.6f px2"
                      % (built[i][0]["slug"], built[j][0]["slug"], a))
                worst = max(worst, a)
    print("  maximum overlap: %.6f px2 (%.8f%% of the triangle)"
          % (worst, 100 * worst / tri_area))

    # coverage
    print("\n" + "-" * 70)
    print("Triangle coverage")
    print("-" * 70)
    union = unary_union(geoms)
    missing = triangle.difference(union).area
    extra = union.difference(triangle).area
    print("  uncovered area      : %.6f px2 (%.6f%%)" % (missing, 100 * missing / tri_area))
    print("  area outside triangle: %.6f px2 (%.6f%%)" % (extra, 100 * extra / tri_area))

    # rules vs polygons, interior points far from the boundaries only
    print("\n" + "-" * 70)
    print("Rules <-> polygons consistency (interior points only, >0.3 percentage points from boundaries)")
    print("-" * 70)
    classes = rules["classes"]
    checked = diff = 0
    ex = []
    step = 0.5
    n = int(round(100 / step))
    for i in range(n + 1):
        ci = i * step
        for j in range(n + 1 - i):
            si = j * step
            sa = 100.0 - ci - si
            if min(sa, si, ci) < 0.5:
                continue
            if distance_to_boundaries(classes, sa, si, ci) < 0.3:
                continue
            r = classify_rules(classes, sa, si, ci)
            if r is None:
                continue
            checked += 1
            x, y = composition_to_xy(sa, si, ci, frame)
            p = Point(x, y)
            hits = [c["slug"] for c, g in built if g is not None and g.covers(p)]
            if len(hits) != 1 or hits[0] != r:
                diff += 1
                if len(ex) < 8:
                    ex.append((sa, si, ci, r, hits))
    print("  points checked   : %d" % checked)
    print("  disagreements    : %d (%.4f%%)" % (diff, 100.0 * diff / max(checked, 1)))
    for e in ex:
        print("    sand=%.1f silt=%.1f clay=%.1f  rule=%s  polygons=%s" % e)

    # known test cases
    print("\n" + "-" * 70)
    print("Test cases")
    print("-" * 70)
    cases = [
        ((100, 0, 0), "sand"),
        ((0, 0, 100), "clay"),
        ((0, 100, 0), "silt"),
        ((70, 20, 10), "sandy_loam"),   # example from the Wikipedia article
        ((40, 40, 20), "loam"),
        ((20, 60, 20), "silty_loam"),
        ((20, 20, 60), "clay"),
        ((50, 25, 25), "sandy_clay_loam"),
        ((30, 40, 30), "clay_loam"),
        ((10, 60, 30), "silty_clay_loam"),
        ((50, 5, 45), "sandy_clay"),
        ((5, 55, 40), "silty_clay"),
        ((92, 6, 2), "sand"),
        ((80, 15, 5), "loamy_sand"),
        ((60, 30, 10), "sandy_loam"),
        ((45, 45, 10), "loam"),
        ((10, 75, 15), "silty_loam"),
        ((5, 90, 5), "silt"),
    ]
    ok = 0
    for (sa, si, ci), want in cases:
        got = classify_rules(classes, sa, si, ci)
        mark = "ok " if got == want else "DIFFERENT"
        if got == want:
            ok += 1
        print("  sand=%3d silt=%3d clay=%3d -> %-18s (expected %s) %s"
              % (sa, si, ci, got, want, mark))
    print("  %d/%d correct" % (ok, len(cases)))


# -------------------------------------------------------------------- output
def write_output(rules, built):
    classes = []
    for cls, geom in built:
        if geom is None:
            continue
        rings = []
        for g in (geom.geoms if isinstance(geom, MultiPolygon) else [geom]):
            rings.append([[round(x, 3), round(y, 3)] for x, y in g.exterior.coords[:-1]])
        lp = label_point(geom)
        classes.append({
            "slug": cls["slug"],
            "en": cls["en"],
            "it": cls["it"],
            "color": cls["color"],
            "source_text": cls["source_text"],
            "rules": cls["clauses"],
            "polygons": rings,
            "label": [round(lp[0], 1), round(lp[1], 1)],
        })

    doc = {
        "system": rules["system"],
        "title": rules["title"],
        "source": rules["source"],
        "notes": rules["notes"],
        "frame": rules["frame"],
        "classes": classes,
    }

    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "classes.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "classes.js"), "w", encoding="utf-8") as fh:
        fh.write("/* Generated by tools/build_dataset.py - do not edit by hand.\n")
        fh.write("   Source: %s, %s */\n" % (doc["source"]["document"], doc["source"]["chapter"]))
        fh.write("window.SOIL_TRIANGLE_DATA = ")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    print("\nWrote data/classes.json and data/classes.js (%d classes)" % len(classes))


def main():
    rules = json.load(open(os.path.join(DATA, "rules.json"), encoding="utf-8"))
    built = build(rules)
    validate(rules, built)
    if "--check" not in sys.argv:
        write_output(rules, built)


if __name__ == "__main__":
    main()
