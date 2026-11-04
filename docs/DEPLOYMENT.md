# Deployment

The hosted system is the same set of containers as the laptop stack, described by
`docker-compose.prod.yml`, with a reverse proxy (Caddy) in front that provides HTTPS.

```
 browser ── 443 ──► Caddy ──► API (uvicorn x2) ──► Postgres 16 (partitioned, append-only raw events)
                                  │                   ▲
                                  ├──► Redis ◄── Celery workers (map/reduce) ──┘
                                  └──► object store (S3-compatible, bulk uploads)
```

Only Caddy listens on a public port. Postgres, Redis and the object store are reachable only on the
internal Docker network. The API refuses to start outside dev with the default `JWT_SECRET`, and
`scripts.bootstrap` refuses to create demo accounts unless `ALLOW_DEMO_SEED=true` is set on purpose and
every `SEED_*_PASSWORD` is non-empty.

## Option A: one VM (recommended)

Any 64-bit Linux VM with Docker works. The target is **Oracle Cloud Always Free**
(Ampere A1: up to 4 OCPU / 24 GB RAM, free): enough for the full 1,000,000-event demo and four workers. All the
images used (python, postgres, redis, caddy, seaweedfs) are published for ARM and x86.

1. **Create the VM.** Ubuntu 22.04 or 24.04, shape `VM.Standard.A1.Flex` (2-4 OCPU, 12-24 GB), a public IP, and
   your SSH key.
2. **Open the cloud firewall.** In the VM's VCN Security List add ingress rules for TCP **80** and **443**
   from `0.0.0.0/0`. (Oracle blocks them by default; the VM's own firewall is handled by the script.)
3. **Run the setup script** on the VM:

   ```bash
   git clone https://github.com/apratim-1407/hospital-event-analytics.git
   cd hospital-event-analytics
   ./deploy/setup-vm.sh            # HTTPS on <ip>.sslip.io, no domain needed
   # or:  ./deploy/setup-vm.sh demo.example.com   (a domain whose A record points at the VM)
   ```

   It installs Docker if missing, writes `.env` with freshly generated secrets (mode 600), opens 80/443 on the
   VM, builds and starts the stack, generates and loads the demo dataset, runs one full analysis, and prints
   the link and the three logins. Expect 10-15 minutes the first time, mostly the image build.

4. **Share the link.** Give people the **viewer** login (read-only). Keep the admin login private: it can
   run analyses and manage API keys.

Day-to-day:

```bash
docker compose -f docker-compose.prod.yml ps                       # status
docker compose -f docker-compose.prod.yml logs -f api worker       # logs
docker compose -f docker-compose.prod.yml up -d --scale worker=4   # more workers
git pull && docker compose -f docker-compose.prod.yml up -d --build   # update
./deploy/seed-demo.sh                                              # reload the demo dataset
```

Everything has `restart: unless-stopped`, so the system comes back after a reboot. (The laptop stack
deliberately does not restart workers, so the worker-kill demo shows the survivors finishing the job.)

## Option B: try the hosted configuration on your own machine

This is the same stack, with strong generated passwords, listening on port 80:

```bash
cp deploy/.env.production.example .env.prod   # fill POSTGRES_PASSWORD, JWT_SECRET and the SEED_*_PASSWORD values
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build --scale worker=4
ENV_FILE=.env.prod ./deploy/seed-demo.sh
# open http://localhost/
```

It uses its own project name (`hospital-prod`) and volumes, so it does not touch the laptop stack.
A temporary public link to a machine at home can be made with any HTTP tunnel, but it only lives while the
machine, Docker and the tunnel are running.

## Settings that matter

| Variable | Purpose |
|---|---|
| `SITE_ADDRESS` | A domain (automatic HTTPS), `<ip-with-dashes>.sslip.io`, or `:80` for plain HTTP |
| `JWT_SECRET`, `POSTGRES_PASSWORD` | Required; the stack will not start without them |
| `SEED_ADMIN/ANALYST/VIEWER_PASSWORD` | Required while `ALLOW_DEMO_SEED=true` |
| `API_WORKERS`, `WORKER_CONCURRENCY`, `--scale worker=N` | Capacity of the API and of the processing tier |
| `PG_SHARED_BUFFERS` | Lower to `256MB` on a VM with 2 GB of RAM or less |

For a real (non-demo) deployment, set `ALLOW_DEMO_SEED=false`, create the first administrator with
`scripts.create_admin`, and mint ingest keys through the admin API.

## What is not covered

* Postgres is a single container on a local volume. Back it up (`pg_dump`) before relying on it; a managed
  database would be the next step for real hospital data.
* Redis persistence is off on purpose (all state in it is reproducible or short-lived).
* There is one VM, so no high availability.
