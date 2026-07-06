# DragonAssistant

A Discord economy/mining simulator bot, built with `discord.py` and SQLite.

## Project structure

```
dragonassistant/
├── bot.py                    # Entry point - run this to start the bot
├── config.py                 # Loads settings from .env
├── requirements.txt          # Python dependencies
├── .env.example               # Template for secrets (copy to .env)
├── dragonassistant.service    # systemd unit file for running as a background service
├── database/
│   ├── db.py                  # Async-safe SQLite wrapper
│   └── schema.sql              # Table definitions
├── data/
│   └── materials.py            # Game balance data (drop rates, recipes, drill stats)
└── cogs/                       # One file per command group ("cog" = discord.py's plugin unit)
    ├── setup.py                 # /setup mine, /setup currency, /setup fee
    ├── economy.py                # /balance, chat-mining payout loop
    ├── mining.py                  # /mine place, /mine status, /collect
    ├── furnace.py                  # /furnace smelt
    └── factory.py                   # /factory craft
```

## Part 1: Create the LXC container on Proxmox

An LXC container is a lightweight Linux environment that shares the Proxmox host's kernel (unlike a full VM, which emulates its own). It's the right choice for a small always-on bot like this - low overhead, fast to create.

1. In the Proxmox web UI, click **Create CT** (top right).
2. **General**: give it a hostname like `dragonbot`, set a root password.
3. **Template**: choose **Ubuntu 24.04** (search the template list; download it first via Proxmox's storage view if it isn't there yet).
4. **Disk**: 4-8 GB is plenty for this bot.
5. **CPU**: 1 core is fine.
6. **Memory**: 512 MB - 1 GB is fine.
7. **Network**: DHCP is fine unless you want a static IP.
8. Finish the wizard, then select the container in the left sidebar and click **Start**.
9. Open a shell to it: click **Console** in the Proxmox UI (or SSH in once you know its IP, shown in the container's **Summary** tab).

## Part 2: Prepare the container

Everything below runs *inside* the LXC container's shell (via the Proxmox Console or SSH).

```bash
apt update && apt upgrade -y
```
`apt` is Ubuntu's package manager. `update` refreshes the list of available package versions; `upgrade -y` installs any pending updates, `-y` auto-confirming prompts.

```bash
apt install -y python3 python3-venv python3-pip git
```
Installs Python 3, `venv` (for creating isolated Python environments - see below), `pip` (Python's package installer), and `git` (to pull this code onto the server, if you push it to a repo).

```bash
useradd -m -s /bin/bash dragonbot
```
Creates a dedicated, non-root Linux user called `dragonbot` to run the bot under. `-m` creates a home directory for it (`/home/dragonbot`), `-s /bin/bash` gives it a normal shell. Running services as a dedicated non-root user limits the damage if the bot is ever compromised.

## Part 3: Get the code onto the server

Copy this entire `dragonassistant/` folder to `/opt/dragonassistant` on the container. The easiest way from your own machine:

```bash
scp -r dragonassistant root@<container-ip>:/opt/
```
`scp` (secure copy) transfers files over SSH. `-r` means recursive (copies the whole folder, not just one file). Run this from your own computer's terminal, not inside the container.

Then, back inside the container:

```bash
chown -R dragonbot:dragonbot /opt/dragonassistant
```
`chown` changes ownership. `-R` applies it recursively to every file/folder inside. This makes sure the `dragonbot` user (not root) owns everything, since that's who will run the service.

## Part 4: Set up the Python environment

```bash
su - dragonbot
```
Switches to the `dragonbot` user (`su` = "substitute user"). Do everything below as this user, not root.

```bash
cd /opt/dragonassistant
python3 -m venv venv
```
Creates a **virtual environment** - an isolated folder containing its own Python interpreter and package installs, separate from the system-wide Python. This means this bot's dependencies never conflict with other Python projects (or the system's own Python tools) on the same machine.

```bash
source venv/bin/activate
```
Activates the virtual environment for your current shell session - after this, `python` and `pip` point at the versions inside `venv/` instead of the system ones. You'll see `(venv)` appear in your terminal prompt.

```bash
pip install -r requirements.txt
```
Installs everything listed in `requirements.txt` (discord.py, python-dotenv) into the virtual environment.

```bash
cp .env.example .env
nano .env
```
Copies the template, then opens it in `nano` (a simple terminal text editor). Fill in your real bot token from the [Discord Developer Portal](https://discord.com/developers/applications) → your application → **Bot** → **Reset Token**. Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

**Important**: in the Developer Portal, under **Bot**, also enable the **Message Content Intent** toggle - the bot's chat-mining feature requires it, and Discord will refuse the connection without it.

### Quick test run

Still as `dragonbot`, with the venv active:
```bash
python bot.py
```
You should see log lines ending in `Logged in as YourBotName#1234`. Press `Ctrl+C` to stop it - we'll hand this off to systemd next so it runs permanently in the background rather than only while this terminal is open.

## Part 5: Run it permanently with systemd

`systemd` is Ubuntu's service manager - it starts your bot on boot and restarts it automatically if it crashes, without you needing to keep a terminal window open.

Exit back to root (`exit` or `Ctrl+D`), then:

```bash
cp /opt/dragonassistant/dragonassistant.service /etc/systemd/system/
```
Copies the service definition into the folder systemd scans for unit files.

Open `/etc/systemd/system/dragonassistant.service` and double check the `User=`, `WorkingDirectory=`, and `ExecStart=` lines match your actual paths (they're already set to `dragonbot` / `/opt/dragonassistant` if you followed the steps above exactly).

```bash
systemctl daemon-reload
```
Tells systemd to re-read unit files from disk, since we just added a new one.

```bash
systemctl enable --now dragonassistant
```
`enable` makes it start automatically on every future boot; `--now` also starts it immediately.

```bash
systemctl status dragonassistant
```
Shows whether it's running (green "active (running)") and the last few log lines.

```bash
journalctl -u dragonassistant -f
```
Streams the bot's live logs (`-f` = "follow", like `tail -f`). `Ctrl+C` to stop watching (the bot keeps running).

To restart after making code changes:
```bash
systemctl restart dragonassistant
```

## Notes on this scaffold

This is a working foundation, not a finished game - a few things are intentionally left as `TODO`s for you to build out as you learn:
- Placing a drill doesn't yet deduct the crafted drill item from your inventory.
- `/collect` currently returns a generic material count rather than a breakdown by material type per drill.
- No DragonCoin earning/spending loop yet (the doc doesn't fully specify how DragonCoin is acquired - worth deciding before building it).
- No admin/moderation safety limits (e.g. rate limiting `/factory craft` spam) beyond what's in the doc.

The background loops (`tasks.loop(...)`) are the core pattern you'll extend most - each is a self-contained async function that fires on a timer, independent of any user command.
