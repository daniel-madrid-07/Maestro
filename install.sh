#!/usr/bin/env bash
# Maestro installer for Linux, macOS, and the inside of a WSL distro.
#
#   curl -fsSL https://raw.githubusercontent.com/daniel-madrid-07/Maestro/main/install.sh | bash
#
# Installs what Maestro needs and nothing else, skipping whatever is already
# there: tmux, git and curl from the system package manager; uv; Claude Code
# (official installer); Maestro itself; then `maestro init`, a Claude sign-in if
# there is none, and `maestro up`. Safe to run again.
#
# Options:
#   --system     only the system packages (needs root or sudo)
#   --user       only the per-user part (uv, Claude Code, Maestro, init)
#   --no-login   do not start the Claude sign-in; just report whether it is needed
#   --no-start   do not start the server at the end
#
# Environment:
#   MAESTRO_SOURCE   what `uv tool install` installs (default: the GitHub repo);
#                    a local path works, for testing.

set -euo pipefail

MAESTRO_SOURCE="${MAESTRO_SOURCE:-git+https://github.com/daniel-madrid-07/Maestro}"
DO_SYSTEM=1 DO_USER=1 LOGIN=1 START=1
for arg in "$@"; do
  case "$arg" in
    --system)   DO_USER=0 ;;
    --user)     DO_SYSTEM=0 ;;
    --no-login) LOGIN=0 ;;
    --no-start) START=0 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
# Run a noisy third-party installer quietly; show its output only if it fails.
quietly() {
  local log; log="$(mktemp)"
  if ! "$@" >"$log" 2>&1; then cat "$log" >&2; rm -f "$log"; return 1; fi
  rm -f "$log"
}
ok()   { printf '    ok  %s\n' "$*"; }
die()  { printf '\n    error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"
IN_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IN_WSL=1

# ------------------------------------------------------------------ system

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"
  elif have sudo; then sudo "$@"
  else die "installing system packages needs root: re-run as root, or install $* yourself"
  fi
}

system_step() {
  step "System packages (tmux, git, curl)"
  local need=()
  for tool in tmux git curl; do have "$tool" || need+=("$tool"); done
  if [ "$OS" = Darwin ]; then
    if [ ${#need[@]} -gt 0 ]; then
      have brew || die "Homebrew is needed on macOS: https://brew.sh"
      brew install "${need[@]}"
    fi
  elif [ ${#need[@]} -gt 0 ] || [ ! -e /etc/ssl/certs/ca-certificates.crt ]; then
    if have apt-get; then
      as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux git curl ca-certificates >/dev/null
    elif have dnf;    then as_root dnf install -y -q tmux git curl ca-certificates
    elif have pacman; then as_root pacman -Sy --noconfirm --needed tmux git curl ca-certificates
    elif have zypper; then as_root zypper -n install tmux git curl ca-certificates
    else die "no supported package manager found; install tmux, git and curl yourself"
    fi
  fi
  for tool in tmux git curl; do have "$tool" || die "$tool is still missing"; done
  ok "$(tmux -V), $(git --version | cut -d' ' -f3), curl"

  # Claude Code draws its state with Unicode glyphs, and Maestro reads the
  # screen to know what an agent is doing: a non-UTF-8 locale breaks both.
  if [ "$OS" = Linux ] && ! locale 2>/dev/null | grep -qi 'utf-\?8'; then
    as_root sh -c 'printf "export LANG=C.UTF-8\nexport LC_ALL=C.UTF-8\n" > /etc/profile.d/maestro-utf8.sh'
    ok "UTF-8 locale set for login shells"
  fi
}

# ------------------------------------------------------------------ user

path_add_local_bin() {
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
  # Login shells (and `wsl -- bash -lc`, which is how Windows reaches Maestro)
  # must find ~/.local/bin too.
  local line='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
  for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zprofile"; do
    [ -f "$rc" ] || [ "$rc" = "$HOME/.profile" ] || continue
    grep -qF '.local/bin' "$rc" 2>/dev/null || printf '\n# added by the Maestro installer\n%s\n' "$line" >> "$rc"
  done
}

claude_logged_in() {
  claude auth status --json 2>/dev/null | grep -q '"loggedIn": *true'
}

user_step() {
  [ "$(id -u)" -ne 0 ] || die "run the per-user part as your own user, not root (Claude Code refuses to run its agents as root)"
  path_add_local_bin

  step "uv (Python tool installer)"
  if ! have uv; then quietly sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh'; fi
  have uv || die "uv did not install"
  ok "$(uv --version)"

  step "Claude Code"
  if ! have claude || { [ "$IN_WSL" = 1 ] && case "$(command -v claude)" in /mnt/*) true ;; *) false ;; esac; }; then
    # In WSL a Windows install of Claude Code can shadow the Linux one through
    # interop; agents must run the Linux build, so install it regardless.
    quietly bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
    hash -r
  fi
  have claude || die "Claude Code did not install"
  ok "$(claude --version 2>/dev/null | head -1)"

  step "Maestro"
  uv tool install --force --quiet "$MAESTRO_SOURCE"
  have maestro || die "maestro did not install"
  ok "$(maestro --version)"

  step "maestro init"
  maestro init

  step "Claude sign-in"
  if claude_logged_in; then
    ok "signed in"
  elif [ "$LOGIN" = 1 ] && [ -t 0 ]; then
    echo "    A browser opens to sign in to your Claude account."
    claude auth login
    claude_logged_in || die "not signed in; run 'claude auth login' and then 'maestro up'"
    ok "signed in"
  else
    echo "    not signed in yet: run 'claude auth login', then 'maestro up'"
    START=0
  fi

  step "maestro doctor"
  maestro doctor || true

  if [ "$START" = 1 ]; then
    step "Starting Maestro"
    maestro up
  fi
}

[ "$DO_SYSTEM" = 1 ] && system_step
[ "$DO_USER" = 1 ] && user_step
printf '\n\033[1mDone.\033[0m\n'
