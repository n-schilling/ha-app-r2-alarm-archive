# R2 Alarm Archive

Keeps the alarm log of Home Assistant outside the house: every log entry goes to a Cloudflare R2 bucket as soon as it is written, together with a snapshot of the states and camera pictures whenever the alarm panel changes, and a summary of each day. If the Home Assistant machine is stolen or destroyed, the log is still there.

The app reads the log from Home Assistant's recorder and resumes where it stopped, so a restart, an update or an outage loses nothing the recorder still has. Everything waits in a queue on the machine until R2 has confirmed it. Objects are created once and never overwritten; with a bucket lock not even the app's key can delete them.

## How it works

- **Log.** Write the alarm log with `logbook.log` and one anchor entity, e.g. an `input_boolean` (`log_entity`). The app follows the logbook of that entity and puts each entry into its own object: `protokoll/YYYY/MM/DD/<UTC time>_<hash>.json`. The first start reads everything the recorder has.
- **Snapshots.** When the alarm panel (`alarm_panel`) changes to one of the `snapshot_states`, the app stores, under `snapshots/YYYY/MM/DD/<UTC time>_<state>/`:
  - `zustand.json.gz`: the states of `snapshot_entities` (groups with their members, three levels deep) and the panel, or of all entities with `snapshot_all_entities`;
  - `kamera_<camera>.jpg`: one picture per entity in `cameras`;
  - `meta.json`: from and to state, the panel's attributes, the user, which pictures are missing.

  Was the app not running at the time, it rebuilds the states from the recorder at its next start, marks the snapshot as `reconstructed` and lists the pictures as missing. Snapshots start with the app's first start.
- **Daily summary.** At 00:05 local time (Home Assistant's time zone) the app writes `zusammenfassung/YYYY/MM/YYYY-MM-DD.txt`: one line per entry of the day before, its snapshots, and the SHA-256 of every entry. It is built from what is in R2, so it shows what really arrived. It waits until nothing of that day is in the queue. Entries that reach R2 after the summary go into `…-nachtrag-1.txt`, `-2` and so on.
- **Folders** follow the local date, times inside the objects are UTC.

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
| `cameras` | | Camera entities, one picture each per snapshot |
| `retention_log`, `retention_snapshots`, `retention_summaries` | `unlimited`, `365`, `unlimited` | Days, or `unlimited`; checked, not set |
| `lock_log`, `lock_snapshots`, `lock_summaries` | `off` | `off`, `indefinite` or days; checked, not set |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | | Only for a broker the Supervisor does not know |
| `log_level` | `info` | `info`, `warning` or `error` |

## Entities

One device *R2 Alarm Archive*:

| Entity | Meaning |
|---|---|
| Archiving | **The one for dashboards.** On while archiving works; off with the reasons in its attribute `problems`: Home Assistant not connected, R2 not reachable, the last upload failed, an object waits longer than 15 minutes, the free tier limit is reached or no usage figure is there, a summary is more than an hour late |
| R2 connection | Off only when R2 cannot be reached (network, R2 itself). An answer with an error, such as a refused key, counts as connected and goes to the log |
| Queue | Objects waiting for R2 |
| Last upload, Last summary | When R2 last confirmed an object, when the last summary was written |
| Free tier usage | The highest of the three figures in % of the free tier; all three as attributes |
| Free tier limit reached | On at 90 %; unknown without a usage figure |
| Bucket rules differ | On when lifecycle or lock rules do not match the options; unknown without `api_token` |

All entities are unavailable while the app is stopped.

For a dashboard, *Archiving* is enough, e.g. as a tile card. A useful automation: notify when *Archiving* has been off for 15 minutes, with its `problems` in the message.

## Troubleshooting

- **`free_tier_guard needs api_token`**: set `api_token`, or turn the guard off.
- **`api_token refused (HTTP 403)`**: the account token lacks *Account Analytics Read* or *Workers R2 Storage Read*.
- **Uploads fail with HTTP 403**: the R2 access key is wrong or not allowed on this bucket.
- **Queue grows, R2 connection on, Free tier limit reached on**: the account is at 90 % of the free tier; see the attributes of *Free tier usage*.
- **No snapshots**: `alarm_panel` empty, or the state is not in `snapshot_states`.
