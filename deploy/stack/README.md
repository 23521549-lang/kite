# Kite on a machine of its own (Docker Compose)

The development machine runs Kite from a Git checkout under systemd (`kite update` pulls and tests the
code). A machine installed from here runs Kite from images instead, and keeps itself up to date:

* **CI** (`.github/workflows/ci.yml`) builds two images for every green commit of `main` and pushes them
  to GitHub's registry: `ghcr.io/<owner>/kite` (gateway, workflow worker, web back end with its page, the
  chat's OpenCode server: one image) and `ghcr.io/<owner>/kite-sandbox` (each task's sandbox). Each gets a
  `sha-<commit>` tag, and the `stable` tag moves to it.
* **kite-stack** on the machine (`kite_stack.py`, run by root through systemd) checks `stable` every
  10 minutes. A new digest: it takes the compose files out of the new image (`releases/<id>/`), pins both
  images by digest, starts them and checks every service. If one is not healthy in 5 minutes, the
  previous release runs again and that digest is not tried again by itself. Nothing is updated while a
  task, a bench run or a chat answer is in progress.

## Services

| Service | What | Reachable at |
|---|---|---|
| `caddy` | HTTPS front, certificate from Let's Encrypt | 443 (and 80 for the certificate) |
| `gateway` | model gateway, Kite's browser for kaggle.com | 127.0.0.1:8787; `/worker/*` through Caddy |
| `worker` | Temporal worker: runs tasks in sandboxes on the host's Docker | — |
| `web` | web back end and page | 127.0.0.1:8790; through Caddy unless `--access private` |
| `opencode` | the chat's OpenCode 2 server (lead agent) | loopback, for the web's Chat tab |
| `temporal`, `postgres` | workflows; Temporal's and the web's databases | 127.0.0.1:7233, 127.0.0.1:5432 |

Kite's services share the host's network and process space, run as uid 1000 and see `/var/lib/kite` at
the same path as the host, so they behave like the development machine: loopback addresses, the
sandboxes' bind mounts, a bench run's lock. Only the worker gets the Docker socket. `/v1/*` and `/kite/*`
of the gateway are never published.

Everything Kite keeps is under `/var/lib/kite` (put a disk of its own there): `kite/` (config, secrets
0600, journal, task records, library cache), `home/` (the chat folder, repositories, OpenCode's
sessions), `postgres/`, `caddy/`, `stack/` (releases, `stack.env` with the machine's own secrets, 0600
root only, and `status.json` the web reads).

## Install

On Ubuntu 22.04 or 24.04, ports 80 and 443 open (SSH only for you):

```sh
curl -fsSL https://raw.githubusercontent.com/23521549-lang/kite/main/deploy/stack/install.sh | sudo bash
sudo kite-stack admin            # owner account: password, then scan the QR code / type the key in the 2FA app
```

The page is at `https://<ip-with-dashes>.sslip.io/` (or `--domain your.name`). Options:
`--access private` (only `/worker/*` is published; open the page with `ssh -L 8790:127.0.0.1:8790` or
`tailscale serve 8790`), `--https-port`, `--no-auto-update`, `--image ghcr.io/<you>/kite` for a fork.
The installer is safe to run again: it repairs and keeps data and secrets.

On AWS, `infra/aws` makes the machine, its data disk and address, and runs this installer at first boot
(see infra/aws/README.md).

The first time CI publishes the images, make both packages public once (GitHub > your profile >
Packages > kite and kite-sandbox > Package settings > Change visibility > Public): the images hold no
secret and the machine then needs no token. To keep them private, install with
`--ghcr-token-file <file with a token that has read:packages>`.

## Every day

```sh
sudo kite-stack status           # running release, newest seen, last update, containers
sudo kite-stack update           # now (also from the web: Settings > Cập nhật ngay)
sudo kite-stack logs web -f
sudo kite-stack restart gateway  # also from the web after adding a worker (the chat restarts with it)
sudo kite-stack kite doctor      # any kite command, in the gateway's container
sudo kite-stack configure --access private | --domain kite.example.com | --auto-update off
```

### Watchdog: a service that stays unhealthy is restarted by itself

Docker's `restart: unless-stopped` only restarts a container whose process exits — a container
that keeps running while its healthcheck fails (Temporal answering nothing on 7233, for example)
stays broken until someone notices. A second timer, `kite-stack-watchdog`, closes that gap:

* Every **5 minutes** it takes one snapshot (`docker compose ps`) and classifies each service:
  running + healthy is good; running + unhealthy (or stuck starting, exited, dead, restarting)
  is bad; anything else (created, paused, missing) is left alone for the updater or an operator.
* A service is restarted only after being bad on **2 ticks in a row** — a single snapshot cannot
  tell "just started" from "stuck". Between restarts of the same service there is a **15-minute
  cooldown**, and at most **3 restarts per service per rolling hour**. Over either limit the
  watchdog does not restart again; it logs the reason and sends one ntfy push per episode
  (restarted services also get one push). A failed restart (compose itself failed) is retried
  next tick and does not count towards the limit.
* Only the watchdog may restart the infrastructure (`postgres`, `temporal`, `caddy`):
  `kite-stack restart` and the web's request files still refuse them, as before.

State lives in `/var/lib/kite/stack/watchdog.json` (timestamps and service names only, no secrets);
every tick is logged to the journal. Push alerts need `kite notify setup` first — without it the
watchdog logs `chưa nối kênh cảnh báo` and keeps working.

```sh
sudo kite-stack watchdog --check   # only say what would happen, restart nothing
sudo systemctl status kite-stack-watchdog.timer
sudo journalctl -u kite-stack-watchdog.service --since "1 hour ago"
sudo systemctl disable --now kite-stack-watchdog.timer   # turn the watch off (services keep running)
```

A machine installed before this timer existed gets its unit files with the next release, but the
timer itself only starts after `sudo kite-stack install` is run once (safe to re-run: it repairs
and keeps data and secrets).

## Moving from the development machine

On the old machine (it keeps running as the development machine):

```sh
kite stack export                # ~/kite-export.tgz: config, secrets, journal, memory, task records,
                                 # library cache, the chat folder, the repositories of the Repo tab
```

Copy the file over (`scp`), then on the new machine:

```sh
sudo kite-stack import ~/kite-export.tgz
```

The web account comes along (same password and 2FA). The Kaggle notebooks still point at the old address:
`sudo kite-stack kite worker restart <worker>` pushes one with the new address (or paste the cell from
`sudo kite-stack kite worker stub <worker>` into Kaggle). Switch when the new machine works; until then
both can run.
