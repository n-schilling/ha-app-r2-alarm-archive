# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-11-01

### Fixed

- Reading the index from R2 no longer moves the install date back, which would have written empty summaries for days without entries

## [0.6.0] - 2026-11-01

### Added

- With an empty index, e.g. after a new install, the app reads what the bucket holds once: the web page shows the whole archive again and no summary is written twice
- Picture series: options `camera_pictures` (default 3) and `camera_interval` (default 10 s); the first picture at once, the others after it
- Web page: a day or a month as one ZIP, with the log as readable text and a list of files R2 no longer has; a filter for days with an alarm
- Entity *API token expires soon*, on 14 days before `api_token` expires; the date also on the overview

### Changed

- Unreadable queue entries go to `quarantine` in the app's folder in `/addon_configs`, where they can be looked at and deleted; while any lie there, *Archiving* is off and the queue page lists them

## [0.5.1] - 2026-11-01

### Fixed

- Web page: the header takes the height of Home Assistant's header (`--header-height` of the theme), so both line up

## [0.5.0] - 2026-11-01

### Changed

- Web page in the style of Home Assistant's settings: it takes over the theme in use (colors, dark mode, cards, fonts) and opens with a menu with icons: Overview, Details, Queue and Settings, with badges for problems and waiting objects
- *Details* replaces *Days*: the archived days as a table grouped by month, the newest month open, with entries, snapshots, alarms and summaries per day and month
- *Settings* opens the app's options in Home Assistant

## [0.4.2] - 2026-11-01

### Fixed

- Web page: a user without a person (e.g. the login of a wall display) shows with the user's name instead of the ID

## [0.4.1] - 2026-11-01

### Fixed

- Web page: snapshots in a day's list show their full state (`armed_night` showed as `night`); panel states, the user's name and numbers in German appear as such; times no longer wrap, pictures stand side by side; the R2 connection reads "not used since the start" instead of "unknown" right after a start

## [0.4.0] - 2026-11-01

### Added

- A read-only web page in Home Assistant's sidebar (ingress, administrators only, German or English after Home Assistant's language): overview with the free tier in detail (absolute figures for the account and this bucket, projection to the end of the month, storage growth and days until the guard's limit), the archived days with their log, snapshots with states and pictures, the queue; every file can be downloaded
- Objects read for the page are kept in a cache in `/data` (up to 200 MB); reads count against the free tier guard

### Changed

- The free tier usage also tells this bucket's share of operations and storage

## [0.3.1] - 2026-11-01

### Fixed

- Stopping or restarting the app no longer logs the warning `Home Assistant: websocket closed`: the stop itself was mistaken for a lost connection

## [0.3.0] - 2026-11-01

### Added

- Option `camera_states`: only snapshots of these panel states get camera pictures, by default only `triggered`. A picture is about 0.6 MB; with pictures on every arming and disarming the bucket grows by a few hundred MB a month

### Changed

- The Wrangler commands for the bucket rules name the jurisdiction (`--jurisdiction eu`) for a bucket outside the default jurisdiction
- The descriptions of the list options say to type the full entity ID: the Configuration tab has no entity picker for them

## [0.2.1] - 2026-10-31

### Fixed

- A queued object whose description cannot be read is no longer deleted at start; it is moved to `/data/queue/quarantine` and named in the log

## [0.2.0] - 2026-10-31

### Added

- Entity *Archiving* for dashboards: on while archiving works, off with the reasons in its attribute `problems` (Home Assistant not connected, R2 not reachable, last upload failed, an object waiting longer than 15 minutes, free tier limit reached or no usage figure, a summary more than an hour late)

### Changed

- The free tier guard reads the usage right after the month changes, so uploads do not wait for the next 15-minute poll

## [0.1.0] - 2026-10-31

### Added

- First version: the alarm log from the recorder to a Cloudflare R2 bucket, one object per entry, created once and never overwritten
- Snapshots on changes of the alarm panel: states as gzipped JSON, one picture per camera; snapshots the app missed are rebuilt from the recorder
- A summary of the day before, shortly after midnight, built from the objects in R2
- Free tier guard: keeps the whole Cloudflare account 10 % below every limit of R2's free tier
- Checks the bucket's lifecycle and lock rules against the options
- Status entities through MQTT discovery
