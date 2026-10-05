#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "${EUID}" -eq 0 ]; then
  echo "Run this installer as the normal VM user, not root." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Ubuntu/Debian Linux." >&2
  exit 1
fi

echo "==> Installing OS dependencies"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git python3 python3-venv xvfb x11vnc openssh-client

if ! command -v google-chrome >/dev/null 2>&1 && \
   ! command -v google-chrome-stable >/dev/null 2>&1 && \
   ! command -v chromium >/dev/null 2>&1 && \
   ! command -v chromium-browser >/dev/null 2>&1; then
  echo "==> Installing Google Chrome"
  tmp_deb="$(mktemp --suffix=.deb)"
  trap 'rm -f "$tmp_deb"' EXIT
  curl -fsSL -o "$tmp_deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y "$tmp_deb"
fi

echo "==> Preparing Python environment"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

echo "==> Checking the repository path used by the systemd units"
EXPECTED="$HOME/job_application_agent"
if [ ! -e "$EXPECTED" ]; then
  ln -s "$ROOT" "$EXPECTED"
elif [ "$(cd "$EXPECTED" && pwd -P)" != "$(cd "$ROOT" && pwd -P)" ]; then
  echo "The path $EXPECTED already exists and is not this repository." >&2
  echo "Move/rename the existing directory, then rerun this installer." >&2
  exit 1
fi

echo "==> Preparing VNC authentication"
mkdir -p "$HOME/.vnc"
if [ ! -s "$HOME/.vnc/passwd" ]; then
  echo "Create a VNC password for the private SSH-tunneled display."
  x11vnc -storepasswd "$HOME/.vnc/passwd"
  chmod 600 "$HOME/.vnc/passwd"
fi

echo "==> Installing user-level systemd units"
mkdir -p "$HOME/.config/systemd/user"
cp deploy/systemd/job-application-agent-display.service "$HOME/.config/systemd/user/"
cp deploy/systemd/job-application-agent-browser.service "$HOME/.config/systemd/user/"
cp deploy/systemd/job-application-agent-vnc.service "$HOME/.config/systemd/user/"
cp deploy/systemd/job-application-agent.service "$HOME/.config/systemd/user/"
cp deploy/systemd/job-application-agent.timer "$HOME/.config/systemd/user/"

echo "==> Enabling user services after logout/reboot"
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now job-application-agent-display.service
systemctl --user enable --now job-application-agent-browser.service
systemctl --user enable --now job-application-agent-vnc.service
systemctl --user enable job-application-agent.timer

echo
echo "Persistent browser infrastructure is installed."
echo
echo "Next: authenticate Naukri once in the dedicated Chrome profile."
echo "1) Create an SSH tunnel from your own computer:"
echo "   ssh -N -L 5900:127.0.0.1:5900 <vm-user>@<vm-host>"
echo "2) Open a VNC client and connect to localhost:5900."
echo "3) In the displayed Chrome window, open Naukri -> Login -> Sign in with Google."
echo "4) Finish any Google/Naukri verification manually. Do not paste credentials into this project."
echo "5) Verify the session:"
echo "   $ROOT/.venv/bin/python -m src.cli naukri login"
echo "6) Check scheduler state:"
echo "   $ROOT/.venv/bin/python -m src.cli naukri schedule-status"
echo
echo "The timer wakes the scheduler hourly, but the application policy only permits a run every 72 hours."
echo "Real submissions remain disabled until data/profile.json enables auto_submit."
