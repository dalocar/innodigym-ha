# Installing the InnoGym add-on in Home Assistant

Written from the code, not from an install: nothing here has been run inside a real Home Assistant yet. If a step
does not match what you see, the troubleshooting list at the end is the place to start, and please note what differed.

## Before you start

- Home Assistant OS or Supervised (add-ons need the Supervisor). Container and Core installs cannot run add-ons;
  run the app standalone instead (`docker compose up`).
- The **Mosquitto broker** add-on installed and started, and the **MQTT** integration set up (Settings, Devices &
  services, MQTT). The add-on will not start without the broker service.
- The repository must be reachable from Home Assistant. Home Assistant clones it and builds the add-on on the device,
  so a private GitHub repository will not work; make it public.
- **Keep a copy of `old-account-backup/`.** Your closed vendor account no longer returns any workouts, so those
  files are the only record of your history. You will import them in step 5.

## 1. Add the repository

Settings, Add-ons, Add-on store, the three-dots menu, **Repositories**, paste
`https://github.com/dalocar/innodigym-ha`, Add. Reload the store page; "InnoGym add-ons" appears with **InnoGym**.

## 2. Install and start

Open InnoGym, **Install** (the first install builds the image on your device, which takes a few minutes on a Raspberry Pi), then turn on **Start on boot** and **Show in sidebar**, and **Start**. The log should show:

```
Database ready in /data (0 users)
MQTT: publishing to Home Assistant
Serving on http://0.0.0.0:8787 ...
```

`MQTT: off (no broker settings)` means the Supervisor did not hand over broker details; see troubleshooting.

## 3. Open the panel

Click **InnoGym** in the sidebar. There is no login screen: Home Assistant tells the add-on who you are. Each
Home Assistant user who opens the panel gets their own data. The first visit creates the person, named after the
Home Assistant display name.

## 4. Link your INNODIGYM account (optional)

Training, Sync. Either log in with email and password, or, for an account that can no longer log in, open
**Use a saved token instead** and paste a token that still works. The first sync runs at once and then every
`sync_interval_minutes`. With a closed account expect "0 seen": the vendor returns nothing.

## 5. Import your saved history

Training, Sync, **Import a backup**, choose `records_list.json` and every `record_detail_*.json` from
`old-account-backup/` (select them all at once). You should see "Imported N new". Repeating it is harmless.
One unreadable file stops the whole batch with a message naming that file.

## 6. Check the sensors

Settings, Devices & services, MQTT, Devices: there should be a device **INNODIGYM <your name>** with 11 entities and
one event entity. Examples are in `addon/DOCS.md`. The values update after each sync and a few seconds after you
change anything in the panel.

## Several people

Each person opens the panel from their own Home Assistant login, links their own account or imports their own
files, and gets their own device and entities. Administrators cannot yet view other people's workouts.

## Troubleshooting

| What you see | Likely cause and fix |
|---|---|
| Repository will not add | The repo is private, or the URL is wrong. |
| Install fails while building | Open the add-on's log in the Supervisor. The build needs internet access to pull the Python base image. |
| Add-on stops at start, log mentions the MQTT service | Mosquitto is not installed or not running. Start it, then start InnoGym. |
| `MQTT: off (no broker settings)` | Same cause, or the MQTT integration is not set up. Restart InnoGym after fixing. |
| `mqtt: cannot reach the broker ...` | Broker stopped or renamed. The add-on retries on the next sync and keeps going. |
| `mqtt: broker refused the connection (... user name or password)` | The broker's credentials changed; restart InnoGym so it asks the Supervisor again. |
| Device appears but sensors say "unknown" | No workouts yet, or a new user with nothing imported. Import a backup or log one by hand. |
| Entities have a different prefix than the docs | The entity id follows the person's display name; rename in Home Assistant if you prefer. |
| Panel shows the login screen | The page was opened outside Home Assistant (port 8787 directly). Use the sidebar entry. |
| Sync status "auth: ..." | The vendor token was rejected. Link the account again. |
| Sync status "0 seen" every time | Normal for a closed vendor account. |
| Import says "Could not read X: not valid JSON" | The file is not the raw API reply. Use the original saved files. |

To see what was published, subscribe to `innodigym/#` and `homeassistant/+/innodigym_+/#` in the MQTT integration's
"Listen to a topic" tool.

## Updating and removing

Raise `version` in `innodigym/config.yaml`, push, reload the add-on store, then Update in the add-on page (it rebuilds on the device). The
database in `/data` is kept across updates and included in Home Assistant backups. Uninstalling the add-on deletes
it; the retained discovery messages stay on the broker until you remove the device in the MQTT integration.
