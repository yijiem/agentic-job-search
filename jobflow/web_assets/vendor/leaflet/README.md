# Leaflet 1.9.4

Pinned, unmodified browser distribution of [Leaflet](https://leafletjs.com/),
licensed under BSD-2-Clause. See `LICENSE`.

Source distribution: <https://unpkg.com/leaflet@1.9.4/dist/>
Release source: <https://github.com/Leaflet/Leaflet/tree/v1.9.4>

`leaflet.js`, `leaflet.css`, and standard images are served locally; the app
requires no CDN JavaScript and does not need an API key.

Leaflet manages the city markers and controls. The default vector basemap is described in [the MapLibre vendor notes](../maplibre/README.md). When vector rendering is unavailable, the explicitly labelled fallback requests only visible street tiles from
`https://tile.openstreetmap.org/{z}/{x}/{y}.png`, with standard browser caching
and origin-only referrers. It does not prefetch regions or support offline tile
exports. Geographic tiles are separate from private role records: company names,
job descriptions, contact records, and resume data are never sent to the provider.

Map data is © OpenStreetMap contributors:
<https://www.openstreetmap.org/copyright>. Attribution stays visible on the map.
Provider policy: <https://operations.osmfoundation.org/policies/tiles/>.
The standard tile service is best-effort; the city result list remains available
when the map service is unreachable. A larger hosted deployment should configure
a tile provider whose capacity and usage terms match its expected traffic.
