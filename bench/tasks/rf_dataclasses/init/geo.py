"""几何工具：点/形状以元组与字典传导（重构对象：改为 dataclass）。"""

import math


def circle(r):
    return {"kind": "circle", "r": r}


def rect(w, h):
    return {"kind": "rect", "w": w, "h": h}


def point(x, y):
    return (x, y)


def area(shape):
    if shape["kind"] == "circle":
        return math.pi * shape["r"] ** 2
    return shape["w"] * shape["h"]


def perimeter(shape):
    if shape["kind"] == "circle":
        return 2 * math.pi * shape["r"]
    return 2 * (shape["w"] + shape["h"])


def translate(p, dx, dy):
    return (p[0] + dx, p[1] + dy)


def distance(p1, p2):
    return ((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5
