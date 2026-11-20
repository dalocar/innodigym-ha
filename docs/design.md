# Design: InnoGym Home Assistant add-on

The webapp is a stateless proxy: History is fetched live from the vendor cloud and nothing is kept. The add-on
keeps its own SQLite database, copies every workout out of the cloud, and (later) publishes sensors to Home Assistant.
Interactive prototype of the screens: see the design discussion; decisions below are final.

## Decisions
- Several users, each with their own data, vendor account, weekly goal and sensors. Every table carries `user_id`.
- Sensors through **MQTT discovery** (Mosquitto add-on required, `services: mqtt:need`), one device per user.
- A personal record is a new **heaviest weight** for an exercise (no estimated 1RM).
- Standard library only (`sqlite3`, `threading`), as the rest of `server.py`.

## Phases
1. **Packaging and database** (done): `innodigym/`, `repository.yaml`, `app/db.py`, `app/scripts/import_backup.py`, `tests/test_db.py`.
2. **Sync worker**: per user, `sport/record/list` then `sport/record/detail` for ids in `db.ids_without_sets`; re-fetch the last 7 days; log to `sync_log`.
3a. **Server API and ingress** (done): `app/local_api.py` (`/local/*`), per-user from `X-Remote-User-*`, browser calls `api/...` relatively, proxy injects the user's stored vendor token. Routes below.
3b. **Screens** (done): Training tab (Overview, Workouts, Progress, Sync) in `innodigym/app/static/training.js` + `training-core.js` + `training.css`; shown only when `local/me` answers, login screen skipped; eight languages. Original text: relative URLs for ingress (it serves under a path prefix), History/Progress/notes/manual logging from `/local/*`, per-user from the `X-Remote-User-Id` ingress header.
4. **Home Assistant** (done): `app/stats.py` (mirrors `training-core.js`), `app/mqtt.py` (stdlib MQTT 3.1.1 client, discovery, state, event), hooks in `sync.py` and `server.py`. Broker settings from the Supervisor (`SUPERVISOR_TOKEN`) or `INNODIGYM_MQTT_HOST/PORT/USER/PASSWORD`. Verified against a real Mosquitto 2.1; not yet against a real Home Assistant. Original text: MQTT discovery, `innodigym_workout_finished` event with `user`, volume, kcal, new PRs.

5. **Options users and Hevy** (done): `app/accounts.py` turns the `users:` option into database users (credentials, `ha_user` link to the panel visitor); `sync.py` re-logs in with the stored password when the token is missing or rejected; `app/hevy.py` pushes workouts to Hevy after each sync (`workouts.hevy_id` makes it idempotent, `users.hevy_since` bounds it, `hevy_exercises` maps names to templates). Push only; not yet tried against the real Hevy API.

## Data notes (checked against real records)
- Weights arrive in tenths of kg; stored in kg.
- The vendor's workout volume (`coreData`) is **higher** than the sum of set weight x reps, because the machine reports
  weight per cable and counts both cables for combined exercises (record 22401: 6572 kg vs 4171 kg from sets). The
  database stores both: `workouts.volume_kg` (matches the official app) and `db.sets_volume_kg()`. Per-exercise
  progress uses set weights.
- Vendor `extData` rows have a `sort` field; sets are stored in that order.
- A workout deleted locally is soft-deleted so a sync does not restore it.
- Manual workouts use negative ids so they can never collide with a vendor record id.

## Open
- Can one person see another's workouts? Assumed: Home Assistant admins yes, others no.
- If the other users' vendor accounts are closed too, they can only be imported from backup files.

## `local/*` routes (all JSON, own data only)
| Method | Path | Notes |
|---|---|---|
| GET / PUT | `local/me` | profile, `vendorLinked`, `lastSync`; PUT `{goalPerWeek}` |
| POST | `local/me/vendor-login` | `{email,password}`; token stored server-side, never returned |
| POST | `local/me/vendor-token` | `{token}` for accounts that can no longer log in |
| GET / POST | `local/sync` | last run / queue a run now |
| GET / POST | `local/workouts` | `?since=YYYY-MM-DD&limit=`; POST a manual workout `{date,time?,name,sets:[{exercise,weightKg,reps}]}` |
| GET / DELETE | `local/workouts/<id>` | with sets and note |
| PUT | `local/workouts/<id>/note` | `{text,rpe}` |
| GET | `local/sets` | every set of every workout, `?since=` |
| GET | `local/exercises`, `local/exercises/<name>` | list, and every set of one exercise |
| GET / POST | `local/body-weight` | `{date,kg}` |

Without `X-Remote-User-Id` (standalone) everything belongs to one user, "Local". Cross-user viewing for admins is not built yet.
