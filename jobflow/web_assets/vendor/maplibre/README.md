# MapLibre vector basemap runtime

Pinned browser distributions from the official npm registry:

- `maplibre-gl` **5.24.1**, the maintained v5 release with classic browser/CSP builds.
- `@maplibre/maplibre-gl-leaflet` **0.1.4**, compatible with Leaflet 1.9 and MapLibre v5.

Files are unmodified package contents. Licenses are preserved in
`MAPLIBRE-LICENSE.txt` and `LEAFLET-BRIDGE-LICENSE.txt`.

`maplibre-gl-csp.js` runs a same-origin `maplibre-gl-csp-worker.js`; no remote
JavaScript, inline script, blob worker, or API key is needed. Leaflet owns the
role markers and controls; MapLibre renders the geographic base layer.

## Geographic data

The default light and dark styles are supplied by [OpenFreeMap](https://openfreemap.org/):

- `https://tiles.openfreemap.org/styles/positron`
- `https://tiles.openfreemap.org/styles/dark`

Styles, visible vector/raster tiles, glyphs, and sprites use only the
`https://tiles.openfreemap.org` origin. OpenFreeMap publicly permits commercial
and noncommercial use without an account or API key. Its service is best-effort.
The map keeps OpenFreeMap, OpenMapTiles, and OpenStreetMap attribution visible.
No role, profile, contact, or application data is sent to the tile service.

If WebGL or the vector service fails, the map visibly identifies its fallback
and uses the existing OpenStreetMap standard raster service with OSM attribution.
The accessible city results still work when both providers are unavailable.
Tiles are requested for the visible view only, without bulk downloads.

References checked November 14, 2026:

- [OpenFreeMap quick start](https://openfreemap.org/quick_start/)
- [OpenFreeMap usage and attribution](https://openfreemap.org/)
- [MapLibre GL JS](https://maplibre.org/maplibre-gl-js/docs/)
- [Leaflet bridge](https://github.com/maplibre/maplibre-gl-leaflet)
- [OSM standard tile policy](https://operations.osmfoundation.org/policies/tiles/)
