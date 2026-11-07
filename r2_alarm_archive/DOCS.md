# R2 Alarm Archive

Keeps the alarm log of Home Assistant outside the house: every log entry goes to a Cloudflare R2 bucket as soon as it is written, together with a snapshot of the states and camera pictures whenever the alarm panel changes or an automation asks for one, and a summary of each day. If the Home Assistant machine is stolen or destroyed, the log is still there.

The app reads the log from Home Assistant's recorder and resumes where it stopped, so a restart, an update or an outage loses nothing the recorder still has. Everything waits in a queue on the machine until R2 has confirmed it. Objects are created once and never overwritten; with a bucket lock not even the app's key can delete them.

## How it works

- **Log.** Write the alarm log with `logbook.log` and one anchor entity, e.g. an `input_boolean` (`log_entity`). The app follows the logbook of that entity and puts each entry into its own object: `protokoll/YYYY/MM/DD/<UTC time>_<hash>.json`. The first start reads everything the recorder has.
- **Snapshots.** When the alarm panel (`alarm_panel`) changes to one of the `snapshot_states`, the app stores, under `snapshots/YYYY/MM/DD/<UTC time>_<state>/`:
  - `zustand.json.gz`: the states of `snapshot_entities` (groups with their members, three levels deep) and the panel, or of all entities with `snapshot_all_entities`;
  - `kamera_<camera>.jpg`: pictures of each entity in `cameras`, only when the new state is in `camera_states` (default: only `triggered`; a picture is about 0.6 MB). With `camera_pictures` above 1 the app takes a series: the first picture at once, the others every `camera_interval` seconds as `kamera_<camera>_2.jpg`, `_3` and so on (default: 3 pictures, 10 s apart);
  - `meta.json`: from and to state, the panel's attributes, the user, which pictures are missing and which files the series has.

  Was the app not running at the time, it rebuilds the states from the recorder at its next start, marks the snapshot as `reconstructed` and lists the pictures as missing. Snapshots start with the app's first start.
- **Snapshots on request.** An automation can ask for a snapshot without the panel changing, e.g. when a guest opens a forbidden room while the panel is disarmed. It fires the event `r2_alarm_archive_snapshot`, optionally with a `reason` (text, up to 200 characters are kept):

  ```yaml
  actions:
    - event: r2_alarm_archive_snapshot
      event_data:
        reason: Guest opened the study
  ```

  Each event gives one snapshot in `snapshots/YYYY/MM/DD/<UTC time>_request/`, at the moment the event was fired: the same `zustand.json.gz` as above, a picture series of every camera in `cameras` (`camera_pictures`, `camera_interval`; `camera_states` does not apply), and a `meta.json` with the panel's current state as `from`, `request` as `to` and the `reason`. Two events at the same moment give two snapshots; the second folder's time is one microsecond later. It works also without `alarm_panel`; the states are then those of `snapshot_entities`. The app hears the event only while it is connected to Home Assistant: an event fired while the app is stopped or Home Assistant restarts is lost, and nothing rebuilds it.
- **Daily summary.** At 00:05 local time (Home Assistant's time zone) the app writes `zusammenfassung/YYYY/MM/YYYY-MM-DD.txt`: one line per entry of the day before, its snapshots, and the SHA-256 of every entry. It is built from what is in R2, so it shows what really arrived. It waits until nothing of that day is in the queue. Entries that reach R2 after the summary go into `…-nachtrag-1.txt`, `-2` and so on.
- **Folders** follow the local date, times inside the objects are UTC.
- **A new install.** The app keeps an index of what is in the bucket in `/data`. When that index is empty, e.g. after a new Home Assistant installation, the app first lists the bucket once (a few Class A operations) and fills the index from it. The web page then shows the whole archive again, and the app writes no summary twice.
- **Quarantine.** A queued object the app cannot read after a crash is never deleted: it goes to `quarantine` in the app's folder in `/addon_configs` (reachable with Samba or an editor app), *Archiving* turns off and names it. Look at the files and delete them; the problem goes with them.

## Web page

The app adds a page **Alarm Archive** to Home Assistant's sidebar (for administrators only). It reads, it never changes anything, and it reaches the browser only through Home Assistant, so no port is opened. It looks like Home Assistant's settings, takes over the theme in use (colors, dark mode, cards) and follows Home Assistant's language (German or English). It opens with a menu:

- **Overview:** whether archiving works and why not, the R2 connection, the queue, last upload and summary; the free tier with absolute figures for the whole account and this bucket, the guard's limits, a projection to the end of the month, the storage growth per day and when the guard's limit would be reached; the bucket rules, with the Wrangler commands when they differ.
- **Details:** the archived days as a table grouped by month (the newest month open), with entries, snapshots, alarms and summaries per day and month; a filter shows only days with an alarm (a snapshot of `triggered`; snapshots on request are no alarm). A day shows its log lines, its snapshots and its summary files, a snapshot on request as *on request* with its reason; a snapshot shows the change, the reason of a request, the user, the camera pictures and the states.
- **Queue:** what waits on the machine, and since when; and what lies in the quarantine.
- **Settings:** opens the app's options in Home Assistant.

A badge in the menu counts the problems and the waiting objects.

Every file can be downloaded, e.g. a summary or a picture to hand on. A day or a whole month also comes as one ZIP (the icon in the day's header, the download icon in the month's row): every object under its key, the log of each day as readable text, and a list of files R2 no longer has, e.g. after the retention. Objects in the bucket never change, so the app keeps what it read in a cache in `/data` (up to 200 MB). Reads from R2 count against the free tier guard; at its limit the page shows only what the cache has.

## Free tier guard

R2's free tier is per Cloudflare account, not per bucket: 10 GB-month of storage, 1 million Class A and 10 million Class B operations a month (Standard storage only). With `free_tier_guard` on, the app keeps the **whole account**, including everything else you run on it, **10 % below every one of these limits**:

| | Free tier | The app stops at |
|---|---|---|
| Storage (all buckets) | 10 GB-month | 9 GB |
| Class A operations (PUT, LIST) | 1,000,000 / month | 900,000 |
| Class B operations (GET) | 10,000,000 / month | 9,000,000 |

Every 15 minutes the app reads the account's usage from Cloudflare's GraphQL Analytics and adds its own operations since then, because the analytics lag a few minutes. Before every operation it checks that the operation keeps all three figures below their limit. The month counts from the 1st, 00:00 UTC. Without a usage figure from the last hour (API down, token wrong) nothing is uploaded either.

At the limit **everything stops**, the log too. Nothing is lost: it waits in the queue until the new month starts or storage is freed. In normal use the app needs far less: a few hundred PUTs and some megabytes a month.

## Retention and bucket lock

Each kind of data (log, snapshots, summaries) has its own retention and its own bucket lock. The app **does not set** lifecycle or lock rules: a token that can set a lock can remove it too, and on the Home Assistant machine it would let anybody with access to that machine remove the lock and then delete. The app only checks once a day that the bucket's rules match the options and reports a difference in the entity *Bucket rules differ*, with the Wrangler commands that would fix it in its attributes and in the log. For example, with the default options:

```bash
npx wrangler r2 bucket lifecycle add <bucket> delete-snapshots snapshots/ --expire-days 365
```

A lock keeps objects even past their retention. The options therefore refuse a lock longer than the retention of the same kind.

## Requirements

- Home Assistant with the recorder (default) and an MQTT broker, e.g. the Mosquitto broker app with the MQTT integration
- In Cloudflare:
  1. An R2 bucket for this archive only, storage class **Standard**.
  2. An R2 API token (**R2 → Manage API tokens**) with **Object Read & Write**, limited to this bucket. Its access key ID and secret go into the options.
  3. For the free tier guard and the rule check: an account API token (**My Profile → API Tokens**, or account API tokens) with **Account Analytics Read** and **Workers R2 Storage Read**. Both permissions only read.

## Options

The list options (`snapshot_entities`, `cameras`) have no entity picker in the app's Configuration tab: type the full entity ID and choose *Add custom item*.

| Option | Default | Meaning |
|---|---|---|
| `account_id` | | Cloudflare account ID, 32 hex digits |
| `bucket` | | The R2 bucket |
| `jurisdiction` | `default` | `eu` for a bucket with the EU jurisdiction |
| `access_key_id`, `secret_access_key` | | The R2 API token, Object Read & Write on this bucket |
| `api_token` | | Read-only account token (Account Analytics Read, Workers R2 Storage Read); required while the guard is on |
| `free_tier_guard` | `true` | Keep the account 10 % below the free tier |
| `log_entity` | | Entity of the alarm log, e.g. `input_boolean.alarm_log` |
| `alarm_panel` | | Optional: the `alarm_control_panel` for snapshots |
| `snapshot_states` | all armed states, `triggered`, `disarmed` | Panel states with a snapshot |
| `snapshot_entities` | | Entities in each snapshot; groups count with their members |
| `snapshot_all_entities` | `false` | All entities in each snapshot instead |
| `cameras` | | Camera entities whose pictures go into snapshots |
| `camera_states` | `triggered` | Panel states whose snapshots also get pictures; each must be in `snapshot_states`. Snapshots on request always get them |
| `camera_pictures` | `3` | Pictures per camera and snapshot, 1 to 10; from 2 a series |
| `camera_interval` | `10` | Seconds between the pictures of a series, 1 to 60 |
| `r2_check_interval` | `15` | Minutes between checks of the R2 connection, 1 to 1440; one Class B operation each |
| `retention_log`, `retention_snapshots`, `retention_summaries` | `unlimited`, `365`, `unlimited` | Days, or `unlimited`; checked, not set |
| `lock_log`, `lock_snapshots`, `lock_summaries` | `off` | `off`, `indefinite` or days; checked, not set |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | | Only for a broker the Supervisor does not know |
| `log_level` | `info` | `info`, `warning` or `error` |

## Entities

One device *R2 Alarm Archive*:

| Entity | Meaning |
|---|---|
| Archiving | **The one for dashboards.** On while archiving works; off with the reasons in its attribute `problems`: Home Assistant not connected, R2 not reachable, the last upload failed, an object waits longer than 15 minutes, objects lie in the quarantine, the free tier limit is reached or no usage figure is there, a summary is more than an hour late |
| R2 connection | Off only when R2 cannot be reached (network, R2 itself). An answer with an error, such as a refused key, counts as connected and goes to the log. Checked at start and every `r2_check_interval` minutes, and with every upload |
| Queue | Objects waiting for R2 |
| Last upload, Last summary | When R2 last confirmed an object, when the last summary was written |
| Free tier usage | The highest of the three figures in % of the free tier; all three as attributes |
| Free tier limit reached | On at 90 %; unknown without a usage figure |
| Bucket rules differ | On when lifecycle or lock rules do not match the options; unknown without `api_token` |
| API token expires soon | On when `api_token` expires within 14 days, with the date in its attribute `expires_on`; off for a token without expiry; unknown without `api_token`. Checked daily. The R2 key's own expiry cannot be read with these permissions: set a reminder when you give it one |

All entities are unavailable while the app is stopped.

For a dashboard, *Archiving* is enough, e.g. as a tile card. A useful automation: notify when *Archiving* has been off for 15 minutes, with its `problems` in the message.

## Troubleshooting

- **`free_tier_guard needs api_token`**: set `api_token`, or turn the guard off.
- **`api_token refused (HTTP 403)`**: the account token lacks *Account Analytics Read* or *Workers R2 Storage Read*.
- **Uploads fail with HTTP 403**: the R2 access key is wrong or not allowed on this bucket.
- **Queue grows, R2 connection on, Free tier limit reached on**: the account is at 90 % of the free tier; see the attributes of *Free tier usage*.
- **No snapshots**: `alarm_panel` empty, or the state is not in `snapshot_states`.
- **No snapshot on request**: the event type must be exactly `r2_alarm_archive_snapshot`; the app must be running and connected to Home Assistant when the event is fired.
