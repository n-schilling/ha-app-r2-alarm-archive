# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
