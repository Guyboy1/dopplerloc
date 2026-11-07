# dopplerloc

An experimental acoustic OSINT geolocation tool that roughly figures out location from an audio recording that contains audible or hidden doppler curve/s from aircraft, locally using public ADS-B flight history. Local web UI + Python API.

> **Experimental software.** This is not supposed to be genuine software used to accurately locate audio recordings. It was made for fun, experimental and educational purposes. Results are candidate locations, not proof. 

**New here? Install the latest release, unzip the file, then read `SETUP_GUIDE.md`** (step-by-step install for Windows/macOS/Linux, ffmpeg, flight-data sources, OpenSky login, troubleshooting).
Quick version: `python -m pip install -r requirements.txt` -> `python -m dopplerloc.doctor` -> `python -m dopplerloc.web` -> http://127.0.0.1:5000

## RESPONSIBLE USE - read first
Locating a recording can reveal where a person was. Only analyse recordings **you made**, or that you have **explicit permission** to
analyse. Do not use this to find, follow, or identify people without their consent. Not only that, but this is software made just for fun, and because it seemed intresting. Results are statistical estimates and can be wrong.

## Privacy - what this program does with your data
- **What a recording can give away.** The time of a recording plus the aircraft passing overhead can narrow down where it was made, sometimes to tens of metres.
  Video and audio files can also carry their own location, date and device details (the page warns in red if exact coordinates are inside the file). If you share
  recordings, you may want to strip that metadata, and remember that the sound itself can still carry location information.
- **It runs locally.** The web page is served only to your own computer (127.0.0.1). Your recording is not sent to anyone. There is no telemetry and no account.
- **What it stores, and where.** Everything lives in the cache folder (`~/.dopplerloc`, or the folder in the `DOPPLERLOC_CACHE` variable): copies of the recordings
  you upload (kept until you delete them: use "Delete my uploaded recordings" at the bottom of the page), downloaded flight-data archives (the page has a button to
  delete them), the aircraft-type list and cached terrain tiles. Delete the folder to remove everything.
- **What goes over the internet.** Downloads of flight data (adsb.lol via GitHub, or OpenSky if you choose it), terrain tiles from AWS, the aircraft list from
  GitHub, and the web page's map tiles (OpenStreetMap), Leaflet and font files. Terrain-tile requests, OpenSky queries and map tiles reveal to those services
  roughly which area and time you are looking at, but not your recording. The Google and Street View links only open when you click them and contain only coordinates.
- **Your responsibility.** Only analyse recordings you made or have permission to analyse; see "Responsible use" above.

## AI Use
- I like to include AI disclaimers on my repositories. AI was used in the creation of this code. This does not mean it is all AI. Planning, reasoning, pseudocode and a coded draft version of the project was done by me. Then it was improved using AI. 

## Methodology
- Determine the location where an audio or video recording was made by analyzing hidden Doppler signatures of aircraft within the audio material and cross-referencing them with historical ADS-B flight tracking data. The software automatically detects Doppler curves from passing aircraft, reconstructs their flight paths, and compares these acoustic patterns against recorded flight movements to narrow down the geographic location of the recording. It integrates audio signal processing, flight path modeling, terrain data, and various filtering and evaluation techniques to identify potential recording locations. 

- Huge credit to youtuber colsto for figuring out this methodology, his video is very high-quality and goes into depth about how he figured this out: https://www.youtube.com/watch?v=UytYrhYLOKI. I would also recommend his other videos, they are all very entertaining and interesting. This project was made for fun, after watching his video I wanted to create my own simmilar program. 

## What it can and cannot do (compared with the full setup in the video)
| Scenario | Video's full setup | This program | Limits here |
|---|---|---|---|
| Two curves, continuous recording, day known, a state/region | Core method | Yes. Synthetic tests recover the spot to 18-25 m; 3,000 random flights over ~20x20 deg searched in ~21 s | Needs ADS-B for that day. Real audio untested |
| Whole country, day known | Yes (50 GB of data, months of engineering, heavy compute) | Runs (time narrowing, tiles, flight-ring candidates, optional parallel) | Real country-days are far bigger than my tests - start with a state |
| One curve + exact start time | Yes (1 minute of audio, found to a few hundred feet) | Yes. Returns candidates on **both sides** of the flight path (a single curve cannot tell which side), spread along it by your clock tolerance | Needs a seconds-accurate clock |
| Recording with cuts | Not covered (cuts just defeat the offset) | Yes: asks the real clock time at each curve, searches each on its own, keeps places that fit both | You must know the real time at each curve |
| Faint / noisy audio | Zoom + hand-traced line, scored by closeness to the line | Same: click-to-trace, high-resolution view, line scoring; manual picker when auto-detect fails | Real faint audio untested |
| Compressed / phone audio | Shown to survive | Reads any format via ffmpeg (32-bit float) | Untested on real compressed audio |
| Location/time shortcuts | Not part of the video | Reads GPS, place names and time from iPhone, Android, mp3/m4a tags, EXIF/XMP (with exiftool) and file names; warns when exact coordinates exist | Most apps strip metadata |
| Countries | USA | 242 countries pass the full-pipeline self-test and are listed | Whether ADS-B data exists is not measured; moderate/low coverage hints are shown for listed countries and regions |
| Accuracy | "A couple hundred feet" on real data | ~20-50 m on synthetic data | Real-world accuracy unknown |

## How the time rules work
| recording | you give | curves needed |
|---|---|---|
| Continuous, exact start known | day + exact start HH:MM:SS (+- clock uncertainty) | **1** (2 is better) |
| Continuous, start not exact | day, or up to 7 days (time of day optional) | **2** - otherwise the search stops before starting (or type the exact start time) |
| Has cuts | day + start time, then the real clock time at each curve once found | 1 or 2 (two curves are searched independently) |
Clock uncertainty defaults: **5 s** typed, **2 s** from iPhone metadata; both adjustable. Month-only is refused (too broad to search).

## Metadata and the "already viewable" warning
On upload it looks through: ffmpeg/ffprobe tags (iPhone `com.apple.quicktime.*`, Android/MP4 `location`, `creation_time`, mp3/m4a/WAV tags),
exiftool (EXIF GPS, IPTC/XMP place names, GPS clock) if installed, coordinates written as text in comments, and timestamps in file names
(`PXL_20261103_024440123`, `VID_...`). It fills the location/time boxes. If real coordinates are present a warning box says
*"Location is already viewable from the file's metadata"* with the coordinates. A file with only a place name fills country/state/city.
If the file's time is the END of the clip, tick the box and the duration is subtracted. Time-source confidence is shown (iPhone/GPS clock = high).

## The page
Three boxes (file, location, time), Advanced (data source incl. OpenSky login boxes, elevation, temperature, tolerances, workers), Start, a
progress bar with 7 steps and a log, blue manual screens when needed, and results as coordinates + map + charts.
Manual screens: choose/mark aircraft sounds (with resolution control), trace a faint curve (or skip one when allowed), enter real clock time per curve
(cuts), review ambiguous results with overlays and wider-tolerance retry, and confirm big searches.

## Coverage of the video's procedure (audited line by line)
| step in the video | where |
|---|---|
| ADS-B for the day/region (API or scraper, full flight paths) | `providers.py` (OpenSky, adsb.lol, file); fetched with a margin around the box |
| Keep propeller planes by filtering out *known* jet types | `typedb.py` |
| Observer grid around each flight; CPA time per point | `search._flight_cells` (the video's later "ring at the measured distance"), `model.cpa_points` |
| Delayed CPA (sound travel time); slant distance with DEM ground height | `model.cpa_points`, `geo.TerrainTilesDEM` / GeoTIFF |
| Time-offset filter (e.g. 920 s, a few seconds of slack) | `search._tile_task`, `offset_tol_s` |
| Spectrogram; zoom + raise resolution to see faint curves | `audio.make_spec`, manual picker with resolution control |
| Fit the curve -> speed, CPA distance, f0, time; generous ranges | `audio.fit_event` / `fit_points`, `speed_tol`, `dist_tol` |
| Speed filter, then distance-ring candidates | `search._narrow`, `_flight_cells` |
| Silence filter (nobody audible between the events) | `search._tile_task` |
| Model the curve from flight geometry using emission time ("right way") | `model.emission_time`, `ratio_curve` |
| Score = brightness under the modelled curve; event-1 sweeps, event-2 locked | `search.Scorer` |
| 200 m -> 100 m -> finer refinement | `_tile_task`, `locate` |
| Best point per flight pair; scatter of event-1 vs event-2 score | `locate` -> `info["pairs"]`; chart on the result page |
| Compare top vs second-best curves; fine heat maps (event 1 / 2 / combined) | overlay images; `search.heatmap` |
| Check the spot in street view | map + Street View links (the visual match is yours) |
| One flight + a known time | one-curve mode with exact start |
| Faint curve traced by hand, scored by closeness to the line | `Event.trace`, `search._trace_score` |
| Known city/state allows a broad time | location picker + day/week windows |
Beyond the video: recordings with cuts (per-curve clock times), metadata prefill, 242-country picker, manual screens.
Not implemented: the video's full-scale engineering (50 GB / GPU); a third curve; the defence demos (high-pass, drowning out, phone compression) are not procedures.

## Countries
242 countries/regions are listed. Each has a search box and IANA time zones, **and passed `tools/verify_countries.py`**: the full two-curve pipeline run
on a synthetic world at that country's largest city, recovering the true spot to <100 m. Dropped: Fiji and Tuvalu (searches cannot straddle the date line),
and uninhabited/obsolete codes. Box source: hand-set -> Natural Earth -> estimated from cities. State/city boxes are estimated from city positions
(Queensland's misses Cape York) - edit the box if needed. The self-test does **not** know whether ADS-B data exists for a place and date.

## Manual detection (when the program asks you)
A guide ("How to spot an aircraft curve") with a sketch is at the top of the screen. You then go through the recording one ~2-minute section at a time
(Previous / Next), or jump to the places the program thinks may hold an aircraft (stars = strongest), and either accept a suggestion or click the left
and right end of a curve yourself. The picture has labelled time and pitch axes and a "Calm" / "Detailed" switch. Only the instructions for your case are
shown: exact start time (one aircraft), no exact time (two aircraft, or type the exact time), recording with cuts (one or more, clock times asked later).

## Flight-data coverage hints (coverage.py)
ADS-B archives depend on volunteer receivers, so coverage varies. The page shows a notice under the search box and tags the country list.
- **Moderate coverage** (about 64 countries): the big airports and cities are covered, ground level is thin. E.g. Japan, South Korea, China (east), India, Vietnam,
  UAE, Saudi Arabia, Israel, Brazil, Argentina, Chile, Colombia, Mexico, Peru, South Africa, Egypt, Kenya, Morocco, Nigeria, Ethiopia, Russia (west), Belarus.
- **Low coverage** (about 53 countries): Afghanistan, South Sudan, Somalia, Yemen, Syria, Libya, Sudan, Iraq, Iran, Ukraine, Chad, Niger, Mali, Mauritania, DR Congo,
  Central African Republic, Turkmenistan, Uzbekistan, Kazakhstan, Mongolia, Myanmar, Laos, North Korea, Venezuela, Bolivia, Cuba, Papua New Guinea and more,
  plus regions that override the country level: Siberia, the Sahara region, the Amazon basin.
Each shows a short reason. The lists are my estimates, not measured data, and never block a search. The full lists are `COUNTRY` and `ZONES` in `dopplerloc/coverage.py`;
edit them freely. A region zone applies when it holds at least half of your search box.

## Time estimate, download speed, low-storage mode
- The progress bar and the "time left" counter come from the same estimate: bytes still to download / the **measured** download speed (smoothed over the last few
  seconds), overlapped with reading the archive, plus search time per window. Until the speed is measured it uses a rough default and says so. The search part is a
  guess until the first search has run. Time you spend answering manual questions is not counted.
- Download speed-ups: the parts of a day's archive download at the same time; a half-finished download resumes where it stopped; reading starts while later parts
  still download; the archive is read on several CPU cores (`DOPPLERLOC_WORKERS=n` to change, 1 = one core) and `orjson` is used for parsing if installed.
  How much faster this is depends on your connection (if it is already maxed out, only the reading part gets faster) and your CPU; I could not measure real speed-ups here.
- **Low storage mode** (adsb.lol, Advanced): one day (about 3 GB) at a time - download, search, delete - so about 4.5 GB free is enough. By default it also stops as soon as
  a clear winner is found, so remaining days are never downloaded. Note: "checking the results" (step 7) needs the candidates of all days that were searched, so it runs once at the end.
  Without low-storage mode downloaded days stay in the cache folder (repeat runs are fast); the page shows their size and can delete them.

## Scaling to a whole country (search.py)
time window -> speed -> 2-degree tiles (parallel with `workers=N`) -> per-flight ring candidates -> offset filter -> silence filter -> scoring -> 25 m refinement.
One curve uses a cheap vectorised prescore. Cuts mode joins the two curves' candidate cells directly.

## Flight data sources
| source | status |
|---|---|
| Your own parquet/csv (`icao24,time,lat,lon,alt_m[,typecode,is_jet]`) | tested |
| adsb.lol daily archive (github.com/adsblol/globe_history_YYYY; ~3 GB/day) | trace-file parser and part chaining tested on a fake archive; the real download and 2026 archives NOT tested |
| OpenSky Trino via `pyopensky` (account username + password, NOT an API key; typed in the page, settings.conf or env vars; one query per UTC hour) | hand-off and per-hour querying tested with a stub; the real service NOT tested; access is restricted to researchers/government/aviation authorities |

## Tests
    python tests/test_all.py     # 22 end-to-end checks on a synthetic world
    python tests/scale_test.py   # 3,000-flight needle-in-haystack
    python tests/run_seeds.py    # accuracy vs noise
    python tools/verify_countries.py   # rebuilds the supported-country list (~3 min)

## Known limits
- Geometric altitude is ellipsoid height, DEM is geoid height (up to ~50 m apart); constant speed of sound; no wind.
- One- and two-curve searches only. One curve cannot separate the two sides of the flight path.
- Metadata can be missing, wrong or edited; file-name times are wall-clock times in an unknown zone.
- Parallel speed-up was not measurable (single-core test machine).

## Credits and licences
The full list is in the `NOTICE` file; bundled data licences are in `dopplerloc/data/DATA_LICENSES.md`. In short:
- **Places:** dr5hn/countries-states-cities-database (ODbL 1.0) and GeoNames via `geonamescache` (CC BY 4.0). The bundled `places.json.gz` is a derived database under the ODbL.
- **Country boxes / time zones:** sandstrom/country-bounding-boxes (Natural Earth, public domain); IANA `zone.tab`.
- **Aircraft engine types:** derived from ICAO Doc 8643 type designators via Berkanktk/datasets `all_aircrafts.csv` (that project is MIT; its aircraft data is "per source licence"). Only type code, engine type and class are kept. See `NOTICE`.
- **Flight data (downloaded, not bundled):** adsb.lol "globe_history" (ODbL 1.0) and its feeders; format notes from wiedehopf/readsb; aircraft list from wiedehopf/tar1090-db;
  optionally OpenSky Network via `pyopensky` (their terms apply).
- **Elevation (downloaded):** Terrain Tiles on AWS, built from USGS 3DEP, SRTM and GMTED2010 (courtesy of the U.S. Geological Survey), ETOPO1, Copernicus EU-DEM and other national sources.
- **Map and page:** (c) OpenStreetMap contributors (respect the OSM tile usage policy), Leaflet (BSD-2-Clause), Inter font (SIL OFL).
- **Code licence:** see the `LICENSE` file in the repository root.
- Rebuild the bundled data with `tools/build_data.py`.
- **Methodology:** https://www.youtube.com/@colsto, https://www.youtube.com/watch?v=UytYrhYLOKI. 
