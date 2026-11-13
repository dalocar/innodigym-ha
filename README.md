# InnoGym for Home Assistant

A Home Assistant add-on for the INNODIGYM Power X1 Pro training machine. It copies your workouts out of the vendor
cloud into a database of its own (so your history stays yours), shows an overview, workout log and per-exercise
progress for each person, and publishes sensors and a "workout finished" event to Home Assistant over MQTT.

> **Unofficial.** Not affiliated with or endorsed by INNODIGYM or its vendor. It talks to a private,
> reverse-engineered API that can change without notice.

- Install: [docs/install.md](docs/install.md)
- Options and entities: [innodigym/DOCS.md](innodigym/DOCS.md); a dashboard card and automations to paste: [docs/examples/](docs/examples/)
- Design and API: [docs/design.md](docs/design.md)

## Layout

```
repository.yaml          Home Assistant add-on repository manifest
innodigym/               the add-on (config.yaml, Dockerfile, docs)
  app/                   what runs inside it: Python server, sync worker, MQTT, database, web UI (static/)
tests/                   Python tests (node tests for the web UI sit beside the code in app/static/)
docs/                    install guide and design
docker-compose.yml       run it without Home Assistant
```

## Development

```
python3 -m unittest discover -s tests          # Python, standard library only
node --test innodigym/app/static/*.test.js     # web UI logic
docker compose up --build                      # http://localhost:8787, data in ./data
```

## Where the web UI comes from

`innodigym/app/static/` started as a copy of the MyInnoX1 web client (an unofficial X1 Pro client, MIT licensed) and
is now maintained here as a fork. The additions are the Training tab (`training.js`, `training-core.js`,
`training.css`), the language strings for it in every `i18n*.js` pack, and small hooks in `app.js` and `index.html`
(relative `api/` URLs for Home Assistant ingress, add-on detection, no login screen). Improvements to the original
client have to be merged by hand.

## Licence

MIT, see [LICENSE](LICENSE).
