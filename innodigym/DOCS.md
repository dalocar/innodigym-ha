# InnoGym

Copies your INNODIGYM Power workouts into a database that lives in this add-on, so your history, notes and
progress stay yours even if the vendor app changes or your account is closed.

## Requirements

- The **Mosquitto broker** add-on (sensors are published through MQTT discovery).

## Options

| Option | Default | Meaning |
|---|---|---|
| `sync_interval_minutes` | 10 | How often new workouts are fetched (5 to 120). |
| `timezone` | (Home Assistant's) | Optional, e.g. `Europe/Madrid`. Zone of the workout times, used when sending to Hevy. Empty uses the zone set in Home Assistant. |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix; change it only if you changed it in Home Assistant. |

People and their INNODIGYM accounts are added inside the InnoGym panel, not in these options.

### Users

Each person is one entry in `users`. With credentials here you do not need to log in inside the panel, and
the add-on logs in again by itself whenever the INNODIGYM token expires.

| Field | Description |
|---|---|
| `name` | Required. Shown in the panel and used for the sensors. |
| `ha_user` | Home Assistant login name. When this person opens the panel they are linked to this entry. |
| `innogym_email`, `innogym_password` | The INNODIGYM account. Stored in the add-on options (visible to Home Assistant admins and included in backups). |
| `hevy_api_key` | Optional. Hevy API key (Hevy Pro: hevy.com, Settings, Developer). Switches on the push to Hevy. |
| `hevy_actions` | Optional. Choose the Hevy exercise for an InnoGym action: `Squat=Squat (Barbell); Row=Seated Cable Row`. The Hevy side is its exact title or its 8-character id. |
| `hevy_since` | Optional, `YYYY-MM-DD`. Only workouts from this date on are sent to Hevy. Default: the day the key was first added, so your history is not uploaded. |

Users created from the panel keep working; options only add to or update the users they list.

### Hevy

After each sync, new workouts (and manual ones) are created in Hevy once each. Each InnoGym action is matched to a Hevy
exercise in this order: your `hevy_actions` mapping, the exact title, then a title that only adds a bracketed
qualifier when exactly one does ("Row" and "Row (Cable)"). When nothing matches, a custom exercise is created and
remembered (Hevy cannot delete custom exercises through the API; free accounts allow only a few). Weights
are sent as the machine reports them, which for combined exercises is per cable. Times are converted from the
`timezone` option (or Home Assistant's zone) to UTC. Hevy is never read back into InnoGym. This is a personal key used from your own add-on;
Hevy's developer terms require its approval for apps offered to other people.

## Home Assistant entities

Each person gets one device, "INNODIGYM <name>", created through MQTT discovery (retained, so it survives
restarts). Entity ids come from the names, for a person called Alex:

| Entity | Meaning |
|---|---|
| `sensor.innodigym_alex_workouts_this_week` | workouts since Monday |
| `sensor.innodigym_alex_volume_this_week` | kg lifted this week (the vendor's figure) |
| `sensor.innodigym_alex_energy_this_week` | kcal this week |
| `sensor.innodigym_alex_weekly_goal` | workouts per week they aim for |
| `sensor.innodigym_alex_goal_streak` | finished weeks in a row that reached the goal |
| `sensor.innodigym_alex_last_workout`, `_last_workout_date`, `_last_workout_energy`, `_last_workout_volume` | the latest workout |
| `sensor.innodigym_alex_total_volume` | all-time kg |
| `binary_sensor.innodigym_alex_trained_today` | on once a workout is logged today |
| `event.innodigym_alex_workout_finished` | fires when a new workout arrives |

The event carries `user`, `workout_id`, `name`, `started_at`, `volume_kg`, `kcal`, `duration_s` and `new_prs`
(each `{exercise, weight_kg, previous_kg}`). It fires only for workouts that started in the last 48 hours and
never during a person's first sync, so importing a history does not trigger anything.

Example automations:

```yaml
automation:
  - alias: "Gym reminder"
    triggers:
      - trigger: time
        at: "19:30:00"
    conditions:
      - condition: time
        weekday: [mon, wed, fri]
      - condition: state
        entity_id: binary_sensor.innodigym_alex_trained_today
        state: "off"
    actions:
      - action: notify.notify
        data:
          message: "No workout yet today."

  - alias: "Celebrate a personal record"
    triggers:
      - trigger: state
        entity_id: event.innodigym_alex_workout_finished
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.new_prs | length > 0 }}"
    actions:
      - action: light.turn_on
        target: { entity_id: light.living_room }
        data: { color_name: gold, flash: long }
```

Sensors refresh after every sync, after any change made in the panel, and when the add-on starts.

## Where your data is

Everything is in `/data/innodigym.db`, one SQLite file that Home Assistant includes in add-on backups.
Vendor tokens are stored there and never sent to the browser.

## Security

The InnoGym port (8787) must stay unpublished. Home Assistant ingress is the only way in, and it tells the add-on
who is logged in through the `X-Remote-User-*` headers, which the add-on trusts.

## Status

Early version (0.3.2). The database, sync, panel screens and Home Assistant sensors are working; report anything
that does not match this page.

---

Free, independent and unofficial. If it saves you time, [☕ buy me a coffee](https://buymeacoffee.com/dalocar) helps keep it running.
