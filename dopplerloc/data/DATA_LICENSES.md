# Licences of the bundled data files

| file | built from | licence |
|---|---|---|
| `places.json.gz` | dr5hn/countries-states-cities-database (ODbL 1.0) + GeoNames cities (CC BY 4.0) | **ODbL 1.0** (a derived database), attribution to both sources required |
| `country_extra.json` | sandstrom/country-bounding-boxes (Natural Earth, public domain), IANA `zone.tab` (public domain), hand-set boxes | public-domain sources; credit appreciated |
| `supported_countries.json` | produced by `tools/verify_countries.py` from the data above | same as `places.json.gz` |
| `typecodes.csv` | Berkanktk/datasets `all_aircrafts.csv` (from ICAO Doc 8643 designators); only code, engine type and class are kept | the Berkanktk project is MIT, but its aircraft data is "per source license" (ICAO Doc 8643): credit both, and check ICAO's terms before commercial use |

The ODbL covers these data files: if you change `places.json.gz` and share it, share it under the ODbL too and keep the attribution.
The program code is licensed separately (see the LICENSE file in the repository root). See `NOTICE` for the full list of sources.
Rebuild the data with `tools/build_data.py`.
