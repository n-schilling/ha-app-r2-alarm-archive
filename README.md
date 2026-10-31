# R2 Alarm Archive app for Home Assistant

[![Release](https://img.shields.io/github/v/release/n-schilling/ha-app-r2-alarm-archive)](https://github.com/n-schilling/ha-app-r2-alarm-archive/releases)
[![CI](https://github.com/n-schilling/ha-app-r2-alarm-archive/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/n-schilling/ha-app-r2-alarm-archive/actions/workflows/ci.yaml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5?logo=homeassistant&logoColor=white)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

A Home Assistant app (formerly add-on) that keeps the alarm log outside the house: it follows the log in the recorder and puts every entry into a Cloudflare R2 bucket, adds a snapshot of states and camera pictures whenever the alarm panel changes, and writes a summary of each day. Objects are created once and never overwritten; an optional free tier guard keeps the whole Cloudflare account 10 % below the limits of R2's free tier.

## Installation

1. Add the repository to your Home Assistant instance:

   [![Add the repository to My Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fn-schilling%2Fha-app-r2-alarm-archive)

   Or add it manually: **Settings → Apps → Install app → ⋮ → Repositories**, then paste:

   ```text
   https://github.com/n-schilling/ha-app-r2-alarm-archive
   ```

2. Open the app from the app store.
3. Install **R2 Alarm Archive**, fill in the options and start it.

See [the documentation](r2_alarm_archive/DOCS.md) for what to prepare in Cloudflare, the options, entities and troubleshooting. Changes are listed in the [changelog](r2_alarm_archive/CHANGELOG.md).

## Development

The app is a Python package in [`r2_alarm_archive/rootfs/usr/lib/r2_archive`](r2_alarm_archive/rootfs/usr/lib/r2_archive). Tests:

```bash
pip install -r r2_alarm_archive/requirements.txt pytest pytest-aiohttp
pytest
```

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-app-r2-alarm-archive/issues/new/choose) in this repository. Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
