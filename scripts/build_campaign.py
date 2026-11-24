"""Rebuild the study map. Optional dependency: Shapely 2.1 or newer."""
import json
import random
import re
from pathlib import Path

from shapely import constrained_delaunay_triangles, intersection, set_precision, voronoi_polygons
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'web' / 'assets' / 'campaign'
CELL_COUNT = 203
TOLERANCE = 0.02
COORDINATE_GRID = 0.000001
POSITIONS = {
    '荆襄': (376, 373), '淮北': (627, 334), '关中': (275, 274),
    '中原': (490, 267), '齐鲁': (659, 232), '陇右': (154, 234),
    '晋地': (433, 154), '河朔': (566, 145), '燕地': (613, 85),
    '河套': (291, 86),
}


def points(ring):
    return [[round(x, 6), round(y, 6)] for x, y in ring.coords[:-1]]


def polygon_parts(geometry):
    if isinstance(geometry, Polygon):
        if geometry.area > 1e-8:
            yield geometry
    elif hasattr(geometry, 'geoms'):
        for child in geometry.geoms:
            yield from polygon_parts(child)


def decode_outline(path):
    """Read the fixture's compact M/L/Z paths using the even-odd fill rule."""
    geometry = Polygon()
    for ring in re.findall(r'M([^Z]+)Z', path):
        geometry = geometry.symmetric_difference(Polygon([
            [float(value) for value in pair.split(',')] for pair in ring.split('L')
        ]))
    return geometry


def ring_path(ring):
    return 'M' + 'L'.join(
        ','.join(format(value, '.6f').rstrip('0').rstrip('.') for value in pair)
        for pair in points(ring)
    ) + 'Z'


def encode_outline(geometry):
    return ''.join(ring_path(poly.exterior) + ''.join(map(ring_path, poly.interiors))
                   for poly in polygon_parts(geometry))


def choose_seeds(geometry):
    """Space synthetic seeds evenly, with a reproducible irregular outline."""
    rng = random.Random(1127)
    west, north, east, south = geometry.bounds
    points_inside = []
    for y in range(int(north) + 6, int(south), 12):
        for x in range(int(west) + 6, int(east), 12):
            point = Point(x + rng.uniform(-4, 4), y + rng.uniform(-4, 4))
            if geometry.contains(point):
                points_inside.append(point)
    named = {min(points_inside, key=Point(*xy).distance): title
             for title, xy in POSITIONS.items()}
    selected = list(named)
    remaining = {point: min(point.distance(seed) for seed in selected)
                 for point in points_inside if point not in named}
    while len(selected) < CELL_COUNT:
        seed = max(remaining, key=lambda point: (remaining[point], point.x, point.y))
        selected.append(seed)
        del remaining[seed]
        for point, best in remaining.items():
            remaining[point] = min(best, seed.distance(point))
    return selected, named


def make_cells(land, lost):
    seeds, named = choose_seeds(lost)
    # Find the source point before clipping, where it is guaranteed to lie
    # strictly inside its own Voronoi polygon.
    regions = voronoi_polygons(MultiPoint(seeds), extend_to=land.envelope)
    assignments = sorted(((next(seed for seed in seeds if region.contains(seed)), region)
                          for region in regions.geoms), key=lambda item: (-item[0].y, item[0].x))
    cells = []
    visible_areas = []
    for number, (seed, region) in enumerate(assignments, 1):
        visible = intersection(region, lost, grid_size=COORDINATE_GRID)
        triangles = [points(piece.exterior) for polygon in polygon_parts(visible)
                     for piece in constrained_delaunay_triangles(polygon).geoms]
        area = sum(Polygon(triangle).area for triangle in triangles)
        assert area > 0 and abs(area - visible.area) < TOLERANCE
        cells.append(dict(id=f'region-{number:03}', ordinal=number,
                          name=named.get(seed, f'分区 {number:03}'), label=named.get(seed, ''),
                          x=round(seed.x, 6), y=round(seed.y, 6),
                          path=encode_outline(visible), area=round(area, 8), pieces=triangles))
        visible_areas.append(visible)

    union = unary_union(visible_areas)
    total_area = sum(cell['area'] for cell in cells)
    assert len(cells) == CELL_COUNT
    assert abs(total_area - lost.area) < TOLERANCE
    assert union.symmetric_difference(lost).area < TOLERANCE
    assert abs(sum(region.area for region in visible_areas) - union.area) < TOLERANCE
    return cells, round(total_area, 8)


def main():
    data = json.loads((ASSETS / 'outline.json').read_text(encoding='utf-8'))
    land = set_precision(decode_outline(data['landPath']), COORDINATE_GRID)
    base = set_precision(decode_outline(data['basePath']), COORDINATE_GRID)
    cells, total_area = make_cells(land, land.difference(base))
    milestones = json.loads((ASSETS / 'milestones.json').read_text(encoding='utf-8'))
    data.update(version=2, id='shaosong-reader-study-map', title='山河复归图',
                disclaimer='底图为现代自然地理轮廓。地图分区和推进路线用于展示复习进度。',
                cells=cells, totalCells=len(cells), totalLostArea=total_area,
                milestones=milestones)
    output = ROOT / 'web' / 'campaign-data.json'
    output.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'{len(cells)} regions, {len(milestones)} milestones, {total_area:,.2f} square SVG units')


if __name__ == '__main__':
    main()
