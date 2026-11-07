# dopplerloc - installation and user guide

Everything you need, step by step. If something goes wrong, run the checker first:

    python -m dopplerloc.doctor

It tells you exactly what is missing and how to fix it.

---------------------------------------------------------------------------------------------------

## 1. Install (Windows)

1. **Python 3.10 or newer.** Download from https://www.python.org/downloads/ . In the installer tick
   **"Add python.exe to PATH"** before clicking Install. (Check afterwards: open *Command Prompt* and type `python --version`.)
2. **Unzip `dopplerloc.zip`** somewhere simple, e.g. `C:\dopplerloc`. You should see a folder containing `requirements.txt`
   and a folder called `dopplerloc`.
3. Open **Command Prompt**, go into that folder and install the packages:

       cd C:\dopplerloc
       python -m pip install -r requirements.txt

   (If `python` is not recognised, try `py -m pip install -r requirements.txt` and use `py` instead of `python` below.)
4. Check the setup: `python -m dopplerloc.doctor`  - everything required should say `[OK]`.
5. Start it: `python -m dopplerloc.web` and open **http://127.0.0.1:5000** in your browser. Leave the Command Prompt window open
   while you use it (Ctrl+C there stops the program).

## 1b. Install (macOS)

    # Python 3.10+ : https://www.python.org/downloads/  (or: brew install python)
    cd ~/Downloads/dopplerloc          # wherever you unzipped it
    python3 -m pip install -r requirements.txt
    python3 -m dopplerloc.doctor
    python3 -m dopplerloc.web          # then open http://127.0.0.1:5000

## 1c. Install (Linux)

    sudo apt install python3 python3-pip        # or your distribution's equivalent
    cd dopplerloc && python3 -m pip install -r requirements.txt
    python3 -m dopplerloc.doctor && python3 -m dopplerloc.web

---------------------------------------------------------------------------------------------------

## 2. "ffmpeg not found" - what it means and how to fix it

The program uses **ffmpeg** to pull the audio out of your video/audio file. The earlier version needed you to install it
yourself. This version bundles one, so the fix is simply to install the packages from step 3 above:

    python -m pip install -r requirements.txt        (or just:  python -m pip install imageio-ffmpeg)

then **restart the program** (stop it with Ctrl+C and start it again). The page shows a red "Setup problem" box at the top
if ffmpeg is still missing, and `python -m dopplerloc.doctor` explains it.

If you prefer a system-wide ffmpeg instead:

| system | command |
|---|---|
| Windows | `winget install Gyan.FFmpeg`  (then **close and reopen** Command Prompt so PATH refreshes) |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Check it with `ffmpeg -version`. Both ways work; the program uses the system one if it finds it, otherwise the bundled one.

**Optional - exiftool** (reads more metadata from photos/videos): https://exiftool.org (Windows/macOS installers),
`brew install exiftool`, or `sudo apt install libimage-exiftool-perl`. Not required.

---------------------------------------------------------------------------------------------------

## 2b. "Certificate error" - CERTIFICATE_VERIFY_FAILED

Error text: `URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate>`

**What it means.** dopplerloc downloads some public files (the aircraft-type list, adsb.lol flight data, terrain tiles). Your Python could not check the
website's security certificate, usually because that Python install has no list of trusted root certificates. Nothing is wrong with your recording or the site.

**Fix, in this order** (stop when it works):
1. Update the certificate helpers, then restart dopplerloc (the new version uses your operating system's own certificate store when `truststore` is installed):
   `python -m pip install --upgrade certifi truststore`
2. **macOS, Python from python.org:** Finder -> Applications -> the "Python 3.x" folder -> double-click **Install Certificates.command**.
3. **Work/school network, VPN or an antivirus that scans HTTPS** (it swaps in its own certificate): get your organisation's root certificate as a `.pem` file and point
   dopplerloc at it. Windows: `setx SSL_CERT_FILE "C:\path\to\company-root.pem"` then open a new window. macOS/Linux: `export SSL_CERT_FILE=/path/to/company-root.pem`.
4. **Last resort:** turn certificate checking off for dopplerloc's downloads only (they are public, non-secret files, but you lose protection against a fake server).
   Windows: `setx DOPPLERLOC_INSECURE_SSL 1` then open a new window. macOS/Linux: `export DOPPLERLOC_INSECURE_SSL=1`.

Check it with `python -m dopplerloc.doctor`: the "Internet" lines should show OK. If a download half-finished before the error, nothing needs cleaning up; just run again.

## 3. Flight data - you must choose a source (page -> "Advanced" -> Flight data source)

The method matches the recording against aircraft positions, so it needs flight history (ADS-B) for your place and time.

### A) "My own file (parquet/csv)"  - works today with no account
A table with these columns: `icao24` (aircraft hex id), `time` (unix seconds, UTC), `lat`, `lon`, `alt_m` (metres, ideally GPS/geometric height).
Optional: `typecode` (e.g. C172, B738) or `is_jet` (true/false). Type the full path in the box, e.g. `C:\data\tracks.parquet`.
Make sure it covers the day/area you are searching, plus about 20 km around the search box.

### B) "adsb.lol daily archive"  - no account

*Short disk-space and speed notes:* each UTC day is about 3 GB. If your disk is small, tick **Low storage mode** in the page (Advanced): it downloads one day,
searches it, deletes it, then continues, so about 4.5 GB free is enough. Otherwise downloaded days are kept (the page can delete them).
The parts of each day download at the same time and the archive is read on several CPU cores; `python -m pip install orjson` (already in requirements.txt)
speeds the reading further.
Free public archives. The first time, the program downloads the whole UTC day (about **3 GB**, split in parts) from GitHub into
`~/.dopplerloc/adsblol` (on Windows: `C:\Users\<you>\.dopplerloc\adsblol`), then scans it. Needs ~7 GB free disk and a good connection; later runs of the
same day reuse the files. The program asks you to confirm before a big download. (I tested the file parsing on a fake archive,
not a real 3 GB download.)

### C) "OpenSky Network"  - needs an account AND approval (probably skip this unless you qualify)

**Short answer: this program needs your OpenSky account USERNAME and PASSWORD. It does NOT use an API key.**

OpenSky has two separate things, which is why the website is confusing:

| | What it is | Login it uses | Used by this program? |
|---|---|---|---|
| **"API client" / "get an API key"** (Account page) | The REST API: *live* aircraft positions | `client_id` + `client_secret` (OAuth2; username/password logins for the REST API were switched off on 18 March 2026) | **No.** It cannot give the full history we need. Ignore it. |
| **Historical data ("Trino")** | A database of past ADS-B data, which is what this program queries | Your account **username + password** | **Yes** |

So if you clicked "get an API key", you were on the wrong page for this program. You can leave `client_id` / `client_secret` empty everywhere.

**Who can get access.** OpenSky says Trino access is for university-affiliated researchers, governmental organisations and aviation authorities
(aviation research and incident investigations); private or commercial users must contact them for a licence. Applications are reviewed and can
be declined, and until you are approved your login will simply fail. If you are a hobbyist, expect this may not be granted; **adsb.lol (section B)
needs no account and is the practical choice.** If you do apply, describe your use honestly.

**Steps, if you qualify** (from OpenSky's own Trino page, openskynetwork.github.io/opensky-api/trino.html):
1. Register an account on **https://opensky-network.org** (Sign in -> register) and verify your email address.
2. Go to **My OpenSky -> Request Data Access** (https://opensky-network.org/my-opensky/request-data) and fill in the application. Wait for approval.
3. Give the program your login - pick one:
   * **Type it in the page.** Choose "OpenSky Network" in Advanced and fill in username and password. Used for that run only, never saved.
     Your username is converted to lowercase automatically (OpenSky stores usernames in lowercase).
   * **Save it in pyopensky's settings file (permanent).** Find the folder with
     `python -c "from pyopensky.config import opensky_config_dir; print(opensky_config_dir)"`
     (install first with `python -m pip install pyopensky`). The first time pyopensky runs it creates `settings.conf` there:

         [default]
         username = your_opensky_username_in_lowercase
         password = your_opensky_password
         client_id =
         client_secret =

     Fill in only `username` and `password`; leave the other two empty.
   * **Environment variables:** `OPENSKY_USERNAME` (lowercase) and `OPENSKY_PASSWORD`
     (Windows: `setx OPENSKY_USERNAME you` then reopen the window; macOS/Linux: `export OPENSKY_USERNAME=you`).
   If nothing is saved anywhere, a browser window may open so you can log in to the OpenSky site; use the same account.
   Order pyopensky tries: its settings file -> the `traffic` config -> the environment variables -> browser login.

**Rules OpenSky enforces (the program follows them):** every query must be limited to a time partition, so the program asks for ONE UTC hour per query,
one after the other (the limit is 2 at once); a query may run at most 30 minutes and scan at most 100 GB, so keep the search box small. OpenSky
suspends accounts that repeatedly break these rules.

I could not test the OpenSky path (it needs an approved account), so treat it as untested. If it fails, the log shows OpenSky's message plus a hint.

---------------------------------------------------------------------------------------------------

## 4. Using the page - the three boxes

1. **File** - any audio or video. Click *Upload and read*. The program looks for location and time in the file's metadata
   (iPhone, Android, mp3/m4a tags, camera/EXIF/XMP if exiftool is installed, a timestamp in the file name). Whatever it finds
   fills the next boxes - check them. If the file holds actual **coordinates** a red box appears:
   *"Location is already viewable from the file's metadata"* - you may not need the program.
2. **Location** - country -> state -> city (only countries that passed the pipeline self-test are listed). It fills a search box and time zone;
   edit the box if it is wrong. Smaller box = faster and more precise.
3. **Time** - set year, month, day (needed), and choose:

   | your situation | what to enter | what the program needs |
   |---|---|---|
   | Continuous recording, you know the **exact start** | "exact start time" HH:MM:SS (+ clock uncertainty) | **one** clear curve is enough |
   | Continuous recording, only the day (or up to 7 days) | day, "Search N days"; time of day optional | **two** clear curves |
   | **Edited recording (has cuts)** | "Edited" + start time | one or two curves; **after** they are found it asks the real clock time at each curve |

   Clock uncertainty defaults: 5 s for a time you typed, 2 s when it comes from iPhone metadata. Change it if your clock is less certain.
   If a metadata time marks the *end* of the clip (some apps), tick "the time from the file is the END of the clip".

Then **Start**. The bar shows the 7 steps; the strip at the bottom shows what is happening live. Blue boxes ask for your help
(choose the aircraft sounds, trace a faint curve, give per-curve times, review ambiguous results).
Results appear as coordinates, a map (red = best), score charts and heat maps. Always check the best spot with Street View.

**Where the program keeps files:** `~/.dopplerloc` (uploads, downloaded archives, terrain tiles). Delete the folder to reclaim space.

---------------------------------------------------------------------------------------------------

## 5. Troubleshooting

| message / symptom | fix |
|---|---|
| `ffmpeg not found` | section 2: `python -m pip install imageio-ffmpeg`, then restart |
| `python is not recognized` | reinstall Python with "Add to PATH" ticked, or use `py` |
| `No module named ...` | `python -m pip install -r requirements.txt` |
| `ZoneInfoNotFoundError` / time zone errors (Windows) | `python -m pip install tzdata` |
| `Address already in use` | another copy is running; close it or use `python -m dopplerloc.web 5001` and open port 5001 |
| Page asks for the same things twice | you are running an old copy of the page; re-extract the newest zip (this was a bug in a previous version) |
| Map is blank | the map needs internet (OpenStreetMap + Leaflet). Coordinates and links still work |
| "No location matched" | wrong place/time guess, no ADS-B coverage there, clock off by more than the tolerance, or tolerances too tight - use the "search again with wider tolerances" button |
| `CERTIFICATE_VERIFY_FAILED` during a download | Python lacks root certificates (section 2b) |
| OpenSky login fails | access not yet approved, wrong username/password, or you used an API key instead of your account login (section 3C) |
| "this timeframe needs two curves" | enter the exact start time to use one curve, or mark a second curve |

---------------------------------------------------------------------------------------------------

## 6. Responsible use
Locating a recording can reveal where a person was. Use it only on recordings you made or have explicit permission to analyse.
Do not use it to find, follow or identify people without their consent. Results are estimates and can be wrong.
