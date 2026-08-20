"""Serves the Cook Islands hazard catalog as XYZ raster PNG tiles, reading
directly from a consolidated Zarr store (built by the offline conversion
script from the raw SFINCS `.nc`/`.tif` outputs).

Two families of source, distinguished by which dimension selects a single
2D slice alongside `slr`: probabilistic sources select by `return_period`
(int, years); event-based sources select by `event` (string label — a date
for historical_tcs/swell_driven, a numeric ID for synthetic_tcs).
"""

import io
import os

import numpy as np
import xarray as xr
from django.conf import settings
from django.http import Http404, HttpResponse
from PIL import Image
from pyproj import Transformer

STORE_PATH = os.path.join(settings.BASE_DIR, "data", "hazard_catalog.zarr")
TILE_SIZE = 256
WEB_MERCATOR_R = 6378137.0

# source -> (zarr group under the store root, selector dimension name)
PRODUCTS = {
    "combined": ("probabilistic/combined", "return_period"),
    "tc": ("probabilistic/tc", "return_period"),
    "swell": ("probabilistic/swell", "return_period"),
    "historical_tcs": ("historical_tcs", "event"),
    "synthetic_tcs": ("synthetic_tcs", "event"),
    "swell_driven": ("swell_driven", "event"),
}

_dataset_cache: dict[str, xr.Dataset] = {}
_transformer_cache: dict[int, Transformer] = {}


def _get_dataset(source: str) -> xr.Dataset:
    if source not in PRODUCTS:
        raise Http404(f"unknown hazard source '{source}'")
    if source not in _dataset_cache:
        group, _ = PRODUCTS[source]
        _dataset_cache[source] = xr.open_zarr(STORE_PATH, group=group)
    return _dataset_cache[source]


def _get_transformer(epsg: int) -> Transformer:
    if epsg not in _transformer_cache:
        _transformer_cache[epsg] = Transformer.from_crs(
            "EPSG:3857", f"EPSG:{epsg}", always_xy=True
        )
    return _transformer_cache[epsg]


def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    world_size = 2 * np.pi * WEB_MERCATOR_R
    xmin = -np.pi * WEB_MERCATOR_R + x * world_size / n
    xmax = xmin + world_size / n
    ymax = np.pi * WEB_MERCATOR_R - y * world_size / n
    ymin = ymax - world_size / n
    return xmin, ymin, xmax, ymax


def _render_tile(arr: np.ndarray, attrs: dict, z: int, x: int, y: int) -> Image.Image:
    xmin, ymin, xmax, ymax = _tile_bounds_3857(z, x, y)

    px = np.arange(TILE_SIZE) + 0.5
    xs_3857 = xmin + (px / TILE_SIZE) * (xmax - xmin)
    ys_3857 = ymax - (px / TILE_SIZE) * (ymax - ymin)
    gx, gy = np.meshgrid(xs_3857, ys_3857)

    transformer = _get_transformer(attrs["epsg"])
    data_x, data_y = transformer.transform(gx, gy)

    a, b, _, tx, d, e, _, ty = attrs["model_transformation"][:8]
    det = a * e - b * d
    col = ((data_x - tx) * e - b * (data_y - ty)) / det
    row = (a * (data_y - ty) - d * (data_x - tx)) / det

    ny, nx = arr.shape
    col_i = np.round(col).astype(np.int64)
    row_i = np.round(row).astype(np.int64)
    valid = (col_i >= 0) & (col_i < nx) & (row_i >= 0) & (row_i < ny)

    values = np.full(col_i.shape, np.nan, dtype=np.float32)
    values[valid] = arr[row_i[valid], col_i[valid]]

    rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
    has_water = valid & ~np.isnan(values)
    if has_water.any():
        depth = np.clip(values[has_water], 0.0, 5.0) / 5.0
        rgba[..., 2][has_water] = 255  # blue channel constant
        rgba[..., 1][has_water] = (200 * (1 - depth)).astype(np.uint8)  # lighter for shallow
        rgba[..., 0][has_water] = (120 * (1 - depth)).astype(np.uint8)
        rgba[..., 3][has_water] = 190

    return Image.fromarray(rgba, mode="RGBA")


def hazard_tile(request, source: str, slr: str, selector: str, z: int, x: int, y: int):
    if source not in PRODUCTS:
        raise Http404(f"unknown hazard source '{source}'")
    _, dim = PRODUCTS[source]
    slr_m = float(slr)
    selector_value = int(selector) if dim == "return_period" else selector

    cache_path = os.path.join(
        settings.MEDIA_ROOT, "hazard_tiles", source, slr, selector, str(z), str(x), f"{y}.png"
    )
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return HttpResponse(f.read(), content_type="image/png")

    ds = _get_dataset(source)
    try:
        da = ds["depth"].sel(slr=slr_m, **{dim: selector_value})
    except KeyError as exc:
        raise Http404(f"no data for slr={slr_m}, {dim}={selector_value}") from exc

    image = _render_tile(da.values, ds.attrs, z, x, y)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(png_bytes)

    return HttpResponse(png_bytes, content_type="image/png")
