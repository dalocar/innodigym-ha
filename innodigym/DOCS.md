# InnoGym

Copies your INNODIGYM Power workouts into a database that lives in this add-on, so your history, notes and
progress stay yours even if the vendor app changes or your account is closed.

## Requirements

- The **Mosquitto broker** add-on (sensors are published through MQTT discovery).

## Options

| Option | Default | Meaning |
|---|---|---|
| `sync_interval_minutes` | 10 | How often new workouts are fetched (5 to 120). |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix; change it only if you changed it in Home Assistant. |

People and their INNODIGYM accounts are added inside the InnoGym panel, not in these options.

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

Packaging, database, sync worker and the `local/*` API are in place. The progress screens and the Home Assistant
sensors arrive in later versions.
